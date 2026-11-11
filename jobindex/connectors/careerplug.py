"""CareerPlug career sites (``{account}.careerplug.com/jobs`` and white-label domains).

robots.txt (same file on every account host and on the white-label domains, checked
2026-11-11)::

    Disallow: /apps/   /j/   /auth_results/   /attachments/   /manage/   /admin/
    Disallow: /jobs.js   /account.js

List: GET https://{account host}/jobs?page={n} -> server-rendered list, 30 jobs per page,
      sorted by post date. Each job is ``#job_table a[href="/jobs/{id}"]`` with
      ``.job-title .name``, ``.job-location`` and the columns the account chose:
      ``.job-post-date`` ("MM-DD-YY") and/or ``.job-type`` ("Full Time"); the
      pager's ``a.next_page`` links the next page (a disabled ``span`` on the last one).
      An account without jobs says "There are no jobs posted at this time."
      There is no total count. Because the list is paged and can change while it is read
      (a job inserted or removed on an earlier page shifts the others), a board of more
      than one page is read twice and both passes must list the same jobs in the same
      order; otherwise the crawl fails and is retried later.
Jobs: **list only, no descriptions.** ``/jobs.json`` (the JSON list) is disallowed, and
      every job page ``/jobs/{id}`` redirects to ``/jobs/{id}/apps/new`` under the
      disallowed ``/apps/``. Job pages are therefore never fetched; the stored URL is the
      job page a candidate opens.

Location formats seen: "CA-Tarzana-91356", "ON-St. Catharines-L2M 5V6" (region first) and
"Jacksonville-FL-32207-Orangetheory Fitness San Marco" (city first, then the studio), and
a work mode suffix "OH-Westerville-43081 Hybrid - US" / "... Fully Remote - CA".

Hosts: ``{account}.careerplug.com`` and the white-label domains CareerPlug lists in its
robots.txt sitemaps: ``otrecruit.com`` (Orangetheory), ``careinhomesjobs.com``,
``r365hire.com``, ``apscareerportal.com``. An unknown account redirects to
``app.careerplug.com`` (refused as an error, never read as an empty board).

Board identifier: the account host, e.g. ``fastsigns-careers.careerplug.com``.
Verified on fastsigns-careers.careerplug.com (~620 jobs, 21 pages) and
orangetheory-corporate-studios.otrecruit.com (42 jobs, 2 pages), 2026-11-11.
"""

import logging
import re
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import parse_qs, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, split_url

log = logging.getLogger(__name__)

DOMAINS = (
    "careerplug.com",
    "otrecruit.com",
    "careinhomesjobs.com",
    "r365hire.com",
    "apscareerportal.com",
)
MAX_PAGES = 200  # x 30 jobs
_ACCOUNT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RESERVED = {"www", "app", "api", "help", "support", "blog", "status", "cdn", "assets"}
_JOB_HREF = re.compile(r"^/jobs/(?P<id>\d+)/?$")
_EMPTY = "There are no jobs posted at this time"
_POSTAL = r"(?P<postal>\d{5}(?:-\d{4})?|[A-Z]\d[A-Z] ?\d[A-Z]\d)"
_MODE = r"(?:\s+(?P<mode>Hybrid|Fully Remote|Remote)(?:\s*-\s*[A-Z]{2})?)?"
_REGION_FIRST = re.compile(rf"^(?P<region>[A-Z]{{2,3}})-(?P<city>.+?)-{_POSTAL}{_MODE}$")
_CITY_FIRST = re.compile(rf"^(?P<city>.+?)\s*-(?P<region>[A-Z]{{2}})-{_POSTAL}(?:-(?P<site>.+))?$")
_MODES = {
    "Hybrid": RemoteType.HYBRID,
    "Fully Remote": RemoteType.REMOTE,
    "Remote": RemoteType.REMOTE,
}
_CA_POSTAL = re.compile(r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$")


def _text(node: LexborNode | None) -> str:
    return " ".join(node.text(separator=" ").split()) if node else ""


class Location(NamedTuple):
    raw: str
    city: str | None = None
    region: str | None = None
    country: str | None = None
    remote: RemoteType = RemoteType.UNKNOWN


def parse_location(value: str) -> Location:
    """``"CA-Tarzana-91356"`` -> ("Tarzana, CA 91356", "Tarzana", "CA", "US");
    ``"ON-London-N6H 0B7 Fully Remote - CA"`` also sets the remote type."""
    match = _REGION_FIRST.match(value) or _CITY_FIRST.match(value)
    if match is None:
        return Location(value)
    city = match.group("city").strip()
    city = city.title() if city.isupper() else city  # "JACKSONVILLE"
    region, postal = match.group("region"), match.group("postal")
    country = "CA" if _CA_POSTAL.match(postal) else "US" if postal[0].isdigit() else None
    mode = match.groupdict().get("mode")
    remote = _MODES.get(mode or "", RemoteType.UNKNOWN)
    raw = f"{city}, {region} {postal}" + (f" ({mode})" if mode else "")
    return Location(raw, city, region, country, remote)


def _posted(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%m-%d-%y").replace(tzinfo=UTC)
    except ValueError:
        return None


@register
class CareerPlugConnector(BaseConnector):
    platform = "careerplug"
    company_name_sources = (re.compile(r"^(?P<name>.+?)(?:\s+Careers)?$"),)

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        for domain in DOMAINS:
            if host.endswith("." + domain):
                account = host.removesuffix("." + domain)
                if account in _RESERVED or not _ACCOUNT.match(account):
                    return None
                return host
        return None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}/jobs"

    def max_job_drop(self) -> float | None:
        return get_settings().generic_max_job_drop

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        first = await self._read_pages(board_identifier)
        if len(first[1]) > 1:
            again = await self._read_pages(board_identifier)
            if [j.external_id for j in again[0]] != [j.external_id for j in first[0]]:
                raise ConnectorError(
                    f"careerplug: {board_identifier} changed while its {len(first[1])} pages "
                    "were read; refusing a possibly partial list"
                )
        jobs = first[0]
        log.info("careerplug %s: %d jobs on %d pages", board_identifier, len(jobs), len(first[1]))
        return jobs

    async def _read_pages(self, board: str) -> tuple[list[RawJob], list[int]]:
        """All pages of the list: (jobs in list order, number of jobs on each page)."""
        jobs: dict[str, RawJob] = {}
        sizes: list[int] = []
        url: str | None = self.board_url(board)
        page = 1
        while url is not None:
            if page > MAX_PAGES:
                raise ConnectorError(f"careerplug: over {MAX_PAGES} pages for {board}")
            response = await self.http.get(url)
            if (urlsplit(str(response.url)).hostname or "").lower() != board:
                raise ConnectorError(
                    f"careerplug: {url} redirected to {response.url}; not a CareerPlug account"
                )
            page_jobs, url = self._parse_page(board, response.text, page)
            for job in page_jobs:
                if job.external_id in jobs:
                    raise ConnectorError(
                        f"careerplug: job {job.external_id} listed twice on {board}; "
                        "the list changed while paging"
                    )
                jobs[job.external_id] = job
            sizes.append(len(page_jobs))
            page += 1
        # Every page but the last is full; anything else means the page size changed.
        if len(sizes) > 1 and len(set(sizes[:-1])) != 1:
            raise ConnectorError(f"careerplug: uneven pages {sizes} on {board}")
        return list(jobs.values()), sizes

    def _parse_page(self, board: str, html: str, page: int) -> tuple[list[RawJob], str | None]:
        tree = LexborHTMLParser(html)
        table = tree.css_first("#job_table")
        if table is None:
            if page == 1 and _EMPTY in html:
                return [], None
            raise ConnectorError(f"careerplug: no job list on page {page} of {board}")
        jobs: list[RawJob] = []
        for link in table.css("a[href]"):
            match = _JOB_HREF.match(link.attributes.get("href") or "")
            if match is None:
                continue
            title = _text(link.css_first(".job-title .name")) or _text(link.css_first(".name"))
            location_node = link.css_first(".job-location")
            date_node = link.css_first(".job-post-date")
            type_node = link.css_first(".job-type")
            for node in (location_node, date_node, type_node):
                for label in node.css(".job-row-title") if node else []:
                    label.decompose()
            location = _text(location_node)
            if not title:
                raise ConnectorError(f"careerplug: job without a title on {board} (template?)")
            place = parse_location(location) if location else Location("")
            job_url = f"https://{board}/jobs/{match.group('id')}"
            jobs.append(
                RawJob(
                    external_id=match.group("id"),
                    title=title,
                    location_raw=place.raw or None,
                    city=place.city,
                    region=place.region,
                    country=place.country,
                    remote_type=place.remote,
                    employment_type=normalize_employment_type(_text(type_node) or None),
                    posted_at=_posted(_text(date_node)),
                    source_url=job_url,
                    apply_url=job_url,
                )
            )
        if not jobs and not (page == 1 and _EMPTY in html):
            raise ConnectorError(f"careerplug: page {page} of {board} lists no jobs")
        next_link = tree.css_first(".pagination a.next_page[href]")
        if next_link is None:
            return jobs, None
        next_url = urljoin(f"https://{board}/jobs", next_link.attributes.get("href") or "")
        parts = urlsplit(next_url)
        wanted = str(page + 1)
        if (parts.hostname or "").lower() != board or parse_qs(parts.query).get("page") != [wanted]:
            raise ConnectorError(f"careerplug: unexpected next page {next_url} on {board}")
        return jobs, next_url
