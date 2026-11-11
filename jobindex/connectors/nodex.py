"""Nodex job boards (Nodex Limited, UK; "Fabric" platform; sitemap + JSON-LD).

List: ``/sitemap.xml`` (no Sitemap line in robots.txt) lists every page of the board;
      job pages are ``/job/{slug}`` with ``<lastmod>``. On www.mytraveljobs.co.uk the
      sitemap had 120 job URLs and the search page ``/find-jobs`` said 120 jobs
      (2026-11-11). ``/find-jobs?*`` (the paged search) is disallowed by robots.txt, so
      the sitemap is the list.
Job:  JSON-LD JobPosting (title, description, datePosted, validThrough, jobLocation,
      employmentType, hiringOrganization = the advertising employer). The title carries
      the card's extra columns ("Training Officer | Stansted | Competitive | Full time");
      only the first part is kept.

Boards run on their owners' domains and are recognised by the platform's asset path
``/modules/fabricjobs/`` or the footer link to nodex.co.uk's job-board software page
(+ the page's canonical URL).

Board identifier: the site host. Verified on www.mytraveljobs.co.uk (a board Nodex runs
itself; 120 jobs), 2026-11-11. No customer board is published (nodex.co.uk/portfolio
redirects to /services); others will be found by the fingerprint.
"""

import re
from typing import Any

from jobindex.connectors.base import RawJob
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register

#: "Title | Location | Salary | Type": at least two more " | " parts after the title.
_TITLE_COLUMNS = re.compile(r"^(?P<title>.+?)(?: \| [^|]*){2,}$")


@register
class NodexConnector(HostedBoardConnector):
    platform = "nodex"
    page_fingerprint = re.compile(
        r"/modules/fabricjobs/|nodex\.co\.uk/services/job-board-software", re.I
    )
    job_path = re.compile(r"/job/(?P<id>[a-z0-9][a-z0-9-]*)/?")

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}/sitemap.xml"]

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        match = _TITLE_COLUMNS.match(job.title)
        if match is None:
            return job
        return RawJob(**{**job.as_dict(), "title": match.group("title").strip()})
