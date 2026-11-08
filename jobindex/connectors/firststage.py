"""FirstStage career sites (``{tenant}.firststage.co``): listing page + JSON-LD job pages.

List: GET https://{tenant}.firststage.co/jobs  (Next.js, server-rendered; one page with
      every job, grouped by category, so a job can appear under several categories).
      Job links ``/jobs/{10-char id}/view``; the heading ``<h2>{N} jobs open</h2>``.
      There is no sitemap (404) and robots.txt is 404 (= allowed).
Job:  /jobs/{id}/view, JSON-LD JobPosting (title, description, datePosted, baseSalary,
      hiringOrganization, jobLocation with a plain-string address; no validThrough).
      Unknown ids answer 404 (stored closed).

Count check: the linked job ids are compared with the heading. On the only known live
board (agility-gwg, 2026-11-08) the heading says 80 but the page links 79 distinct jobs
(also with ``?layout=list`` and in the RSC payload), so one job is counted but not
reachable. Up to ``MAX_UNLINKED`` such jobs are tolerated (logged); more, more links
than the count, or no heading at all fail the crawl.

Board identifier: the tenant host (``agility-gwg.firststage.co``). Unknown tenants: 404.
"""

import logging
import re

from jobindex.connectors.base import ConnectorError
from jobindex.connectors.generic_jsonld import HostedBoardConnector, host_of
from jobindex.connectors.registry import register

log = logging.getLogger(__name__)

#: Jobs the heading counts that the page does not link (agility-gwg: 80 vs 79).
MAX_UNLINKED = 1

_COUNT = re.compile(r"<h2[^>]*>\s*(\d+)\s+jobs?\s+open\s*</h2>", re.I)
_LINK = re.compile(
    r"href=[\"']((?:https?://[a-z0-9.-]+)?/jobs/[A-Za-z0-9]{10}/view)(?:[?#][^\"']*)?[\"']",
    re.I,
)


@register
class FirstStageConnector(HostedBoardConnector):
    platform = "firststage"
    native_suffix = ".firststage.co"
    job_path = re.compile(r"/jobs/(?P<id>[A-Za-z0-9]{10})/view/?")

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        url = f"https://{board_identifier}/jobs"
        html = await self._get_text(url)
        counter = _COUNT.search(html)
        if counter is None:
            raise ConnectorError(f"firststage: no job count on {url}; template changed?")
        count = int(counter.group(1))
        urls: dict[str, str] = {}  # job id -> URL
        for link in _LINK.findall(html):
            absolute = link if link.startswith("http") else f"https://{board_identifier}{link}"
            if host_of(absolute) != host_of(board_identifier):
                continue
            external_id = self.job_id(absolute, board_identifier)
            if external_id is not None:
                urls[external_id] = absolute
        unlinked = count - len(urls)
        if unlinked < 0 or unlinked > MAX_UNLINKED:
            raise ConnectorError(
                f"firststage: {board_identifier} reports {count} jobs, links {len(urls)}"
            )
        if unlinked:
            log.warning(
                "firststage %s: reports %d jobs but links %d", board_identifier, count, len(urls)
            )
        return [(link, None) for link in urls.values()]
