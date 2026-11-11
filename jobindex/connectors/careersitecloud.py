"""CareerSiteCloud career sites (Aspen Technology Labs; ``{site}.careersitecloud.com``).

robots.txt on customer sites (checked 2026-11-11 on wpp.careersitecloud.com)::

    User-agent: *
    Allow: /
    Disallow: /log-view-*

(The vendor's own www.careersitecloud.com and unknown subdomains answer 403; customer
sites do not.)

List: GET https://{site}/search (every job) or https://{site}/{view} (a view the site
      defines, e.g. one agency of a group: ``/pep``, ``/akqa``). Server-rendered, 20 jobs
      per page, ``<h1 class="total-jobs">N`` = the number of jobs in the list. Further
      pages are ``/search?qs_search_job=page%3d{n}`` or ``/{view}/{n}`` (0-based) and are
      requested directly for n < ceil(N / page size): the site's own pager stops linking
      after page 50 (its later links point to "/"), although the pages exist. Every page
      must state the same total, every page but the last must be full, and the distinct
      job links must add up to the total; anything else fails the crawl. The bare
      ``/search`` states one job fewer than its own later pages (2,180 vs 2,181 on
      2026-11-11), so the full list starts at ``?qs_search_job=page%3d0``.
      **The order is not stable between requests** (no sort or page-size parameter
      exists): on WPP's full list (2,181 jobs, 110 pages) 37 jobs came back on two pages
      and as many on none; the Ogilvy (171) and VML views failed the same way. A job
      seen on two pages therefore fails the crawl at once. Views that fit on one page
      (Pep, 8) are read atomically; small multi-page views (AKQA, 26 on 2 pages) passed
      repeatedly. Bigger lists fail rather than return a partial list.
      ``/sitemap.xml`` also lists the job pages but lags behind (2,172 job URLs vs. 2,181
      listed on 2026-11-11), so it is not used.
Job:  ``/job-{24 hex id}-{slug}`` -> JSON-LD JobPosting (title, HTML description,
      datePosted, validThrough, employmentType, jobLocation, hiringOrganization = the
      agency/brand).

Custom domains are recognised by the platform's asset hosts (``cloudfront.careersitecloud.com``,
``atl-csc.s3.amazonaws.com``); the board is then the page's host.

Board identifier: the site host, optionally ``/{view}`` (lower case, unescaped):
``wpp.careersitecloud.com`` or ``wpp.careersitecloud.com/pep``.
Verified on WPP's career site, views ``/pep`` (8 jobs) and ``/akqa`` (26), 2026-11-11.
Only one customer site was found (ful.io's list of CareerSiteCloud sites; the others
there are parked or no longer on the platform).
"""

import math
import re
from urllib.parse import quote, unquote, urljoin

from selectolax.lexbor import LexborHTMLParser

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.generic_jsonld import HostedBoardConnector, host_of
from jobindex.connectors.registry import register
from jobindex.normalize import split_url

MAX_PAGES = 500  # x 20 jobs
_SUFFIX = ".careersitecloud.com"
_RESERVED = {"www", "support", "elastiq", "cloudfront", "app", "api", "cdn", "static"}
_SITE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: Single-segment paths that are site pages, not job views.
_NOT_VIEWS = {"search", "browse", "about-us", "sitemap.xml", "robots.txt", "favicon.ico"}
_TOTAL = re.compile(r'class="total-jobs"[^>]*>\s*([\d,]+)')


def split_board(board_identifier: str) -> tuple[str, str | None]:
    """``"wpp.careersitecloud.com/pep"`` -> ("wpp.careersitecloud.com", "pep")."""
    host, _, view = board_identifier.partition("/")
    return host, view or None


@register
class CareerSiteCloudConnector(HostedBoardConnector):
    platform = "careersitecloud"
    native_suffix = _SUFFIX
    page_fingerprint = re.compile(r"cloudfront\.careersitecloud\.com|atl-csc\.s3\.amazonaws\.com")
    job_path = re.compile(r"/job-(?P<id>[0-9a-f]{24})(?:-[^/]*)?/?")

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if not host.endswith(_SUFFIX):
            return None
        site = host.removesuffix(_SUFFIX)
        if "." in site or site in _RESERVED or not _SITE.match(site):
            return None
        view = unquote(segments[0]).lower().strip() if segments else ""
        is_page = len(segments) == 1 or (len(segments) == 2 and segments[1].isdigit())
        if view and is_page and not view.startswith("job-") and view not in _NOT_VIEWS:
            return f"{host}/{view}"
        return host

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        host, view = split_board(board_identifier)
        if view:
            return f"https://{host}/{quote(view)}"
        return f"https://{host}/search"

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return []  # the paged list is the source (see module docstring)

    @staticmethod
    def list_url(board_identifier: str, page: int) -> str:
        """URL of list page ``page`` (0-based)."""
        host, view = split_board(board_identifier)
        if view:
            return f"https://{host}/{quote(view)}" + (f"/{page}" if page else "")
        return f"https://{host}/search?qs_search_job=page%3d{page}"

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        urls: dict[str, str] = {}  # job id -> URL
        total, size = await self._read_page(board_identifier, 0, urls, None)
        pages = 1 if total <= size else math.ceil(total / size)
        if pages > MAX_PAGES:
            raise ConnectorError(f"careersitecloud: {pages} pages for {board_identifier}")
        for page in range(1, pages):
            _, listed = await self._read_page(board_identifier, page, urls, total)
            if page < pages - 1 and listed != size:
                raise ConnectorError(
                    f"careersitecloud: page {page} of {board_identifier} lists {listed} jobs, "
                    f"not {size}; the list changed while paging"
                )
        if len(urls) != total:
            raise ConnectorError(
                f"careersitecloud: {board_identifier} reports {total} jobs, "
                f"{len(urls)} listed; refusing a partial list"
            )
        return [(link, None) for link in urls.values()]

    async def _read_page(
        self, board_identifier: str, page: int, urls: dict[str, str], total: int | None
    ) -> tuple[int, int]:
        """Add the jobs of one list page to ``urls``; returns (stated total, jobs listed)."""
        url = self.list_url(board_identifier, page)
        host, _ = split_board(board_identifier)
        response = await self.http.get(url)
        if host_of(str(response.url)) != host_of(host):
            raise ConnectorError(f"careersitecloud: {url} redirected to {response.url}")
        counter = _TOTAL.search(response.text)
        if counter is None:
            raise ConnectorError(f"careersitecloud: no job count on {url}; not a job list?")
        count = int(counter.group(1).replace(",", ""))
        if total is not None and count != total:
            raise ConnectorError(f"careersitecloud: job count changed while paging {url}")
        tree = LexborHTMLParser(response.text)
        listed = 0
        for link in tree.css(".job-listing-full li h3 a[href], .job-listing li h3 a[href]"):
            absolute = urljoin(url, link.attributes.get("href") or "")
            external_id = self.job_id(absolute, board_identifier)
            if external_id is None:
                continue
            listed += 1
            if external_id in urls:
                # The list's order is not stable between requests (WPP's full list of
                # 2,181 jobs: 37 jobs on two pages, as many on none): refuse it.
                raise ConnectorError(
                    f"careersitecloud: job {external_id} listed on two pages of "
                    f"{board_identifier}; the list order changed while paging"
                )
            urls[external_id] = absolute
        if count and not listed:
            raise ConnectorError(f"careersitecloud: page {page} of {board_identifier} is empty")
        return count, listed
