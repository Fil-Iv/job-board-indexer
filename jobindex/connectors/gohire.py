"""GoHire job boards (``jobs.gohire.io/{company}-{board id}``): paged board + JSON-LD pages.

List: GET https://jobs.gohire.io/{board}/?page={n}   (n = 1, 2, ...; 10 jobs per page)
      Server-rendered; job links ``/{company}-{board id}/{title}-{job id}/`` and
      ``<p class="gohire-job-pagination-results">Page 1 of 2, Total 16 jobs</p>``.
      Pages are read until the reported total is reached; any other count is an error
      (the board's own paging buttons POST a form; ``?page=`` GETs return the same pages).
      The site-wide ``/sitemap.txt`` (~17,000 URLs) is not used: it misses current jobs
      (none of Dexerto's 16, 2026-11-07) and keeps old ones. Unknown boards: 404.
      A board without the counter raises (no empty board was available to verify).
Job:  JSON-LD JobPosting (title, description, hiringOrganization, jobLocation,
      jobLocationType, employmentType, baseSalary, datePosted, identifier = job id).
      ``validThrough`` is ignored: listed open jobs carry past dates (2026-09-12 on a job
      still listed on 2026-11-07). Closed jobs answer 404.
      Older postings that are still listed have no JSON-LD at all (4 of 15 on Dexerto,
      2026-11-08): they are mapped from the page markup (``parse_page_html``: ``<h1>``
      "{title}  at {company}", the Location / Job Type / Date Posted items, the
      ``.jp-text`` description).
      Generic sitemap + JSON-LD machinery (``generic_jsonld``): pages are fetched for new
      jobs and every GENERIC_REFRESH_DAYS.

Board identifier: the board path segment, lower case (``dexerto-de5jlhjo``). A board can be
addressed by its id alone (``jobs.gohire.io/su9lyitg``), which redirects to the full slug
(``axented-su9lyitg``) and drops ``?page=``: later pages use the slug page 1 resolved to,
and job URLs are matched on the trailing board id.

URL formats recognised:
    jobs.gohire.io/{board}[/{title}-{job id}/]
Custom-domain career pages: links to the above (generic HTML detection).

Verified on dexerto-de5jlhjo and su9lyitg (Axented), 2026-11-07.
"""

import re
from datetime import UTC, datetime

from selectolax.lexbor import LexborHTMLParser

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register
from jobindex.normalize import normalize_employment_type, split_url

HOST = "jobs.gohire.io"
MAX_PAGES = 500  # x 10 jobs
_BOARD = re.compile(r"^[a-z0-9][a-z0-9-]{0,150}$")
_RESERVED = {"assets", "api", "sitemap.txt", "robots.txt"}
_JOB = re.compile(r"^(?P<title>.*)-(?P<id>\d+)$")
_TOTAL = re.compile(
    r"Page\s*(?:<[^>]+>\s*)*(\d+)\s*(?:<[^>]+>\s*)*of\s*(?:<[^>]+>\s*)*(\d+)\s*(?:<[^>]+>\s*)*,"
    r"\s*Total\s*(?:<[^>]+>\s*)*(\d+)",
    re.I,
)
_ORDINAL = re.compile(r"(\d+)(?:st|nd|rd|th)\b")
_LINK = re.compile(r"href=[\"']((?:https?://jobs\.gohire\.io)?/[^\"'?#]+)[\"']", re.I)


def board_id(board: str) -> str:
    """The trailing board id of a board slug (``dexerto-de5jlhjo`` -> ``de5jlhjo``)."""
    return board.rsplit("-", 1)[-1]


@register
class GoHireConnector(GenericJsonLdConnector):
    platform = "gohire"
    trust_valid_through = False

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        if host != HOST or not segments:
            return None
        board = segments[0].lower()
        if board in _RESERVED or not _BOARD.match(board):
            return None
        return board

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/{board_identifier}/"

    def job_id(self, url: str, board_identifier: str) -> str | None:
        host, segments, _ = split_url(url)
        if host != HOST or len(segments) != 2:
            return None
        if board_id(segments[0].lower()) != board_id(board_identifier):
            return None
        match = _JOB.match(segments[1])
        return match["id"] if match else None

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        urls: dict[str, str] = {}  # job id -> URL
        total: int | None = None
        slug = board_identifier
        pages = 1
        page = 1
        while page <= pages:
            if page > MAX_PAGES:
                raise ConnectorError(f"gohire: over {MAX_PAGES} pages for {board_identifier}")
            url = f"https://{HOST}/{slug}/?page={page}"
            response = await self.http.get(url)
            html = response.text
            if page == 1:
                # ``/su9lyitg/?page=2`` redirects to ``/axented-su9lyitg/`` and loses the
                # page number: later pages must use the slug page 1 resolved to.
                final = split_url(str(response.url))[1]
                slug = final[0].lower() if final else slug
                if board_id(slug) != board_id(board_identifier):
                    raise ConnectorError(f"gohire: {url} redirected to another board")
            counter = _TOTAL.search(html)
            if counter is None:
                raise ConnectorError(f"gohire: no job count on {url}; template changed?")
            if total is not None and int(counter.group(3)) != total:
                raise ConnectorError(f"gohire: job count changed while paging {url}")
            pages, total = int(counter.group(2)), int(counter.group(3))
            for link in _LINK.findall(html):
                absolute = link if link.startswith("http") else f"https://{HOST}{link}"
                external_id = self.job_id(absolute, board_identifier)
                if external_id is not None:
                    urls[external_id] = absolute.rstrip("/") + "/"
            page += 1
        if len(urls) != total:
            raise ConnectorError(f"gohire: {board_identifier} reports {total}, got {len(urls)}")
        return [(url, None) for url in urls.values()]

    def parse_page_html(self, html: str, stub: RawJob) -> RawJob | None:
        tree = LexborHTMLParser(html)
        heading = tree.css_first(".jp-view h1")
        body = tree.css_first(".jp-text")
        if heading is None or body is None:
            return None
        title, _, company = heading.text(strip=True).rpartition(" at ")
        if not title:
            title, company = heading.text(strip=True), ""
        items: dict[str, str] = {}
        for item in tree.css(".jp-header .jp-sidebar-item"):
            key, value = (
                item.css_first(".jp-sidebar-item-title"),
                item.css_first(".jp-sidebar-item-data"),
            )
            if key is not None and value is not None:
                items[key.text(strip=True).lower()] = value.text(strip=True)
        return RawJob(
            external_id=stub.external_id,
            title=title.strip(),
            company=company.strip() or None,
            description_html=body.html,
            location_raw=items.get("location") or None,
            employment_type=normalize_employment_type(items.get("job type")),
            posted_at=_posted(items.get("date posted")),
            source_url=stub.source_url,
            apply_url=stub.source_url,  # "Apply Now" opens a form on the same page
        )


def _posted(value: str | None) -> datetime | None:
    """``"March 2nd, 2026"`` -> 2026-03-02 UTC."""
    if not value:
        return None
    try:
        return datetime.strptime(_ORDINAL.sub(r"\1", value), "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None
