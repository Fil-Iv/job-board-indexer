"""JobBoard.com (HotLizard / Aspire Media Group) job boards (generic sitemap + JSON-LD).

List: robots.txt has no Sitemap line; ``/sitemap.xml`` is an index of ``/xml-sitemaps/{n}.xml``
      listing every page of the board (job pages, location/sector pages, articles).
      Job pages are ``/jobs/{slug}/{id}-{n}/`` (no ``<lastmod>``).
Job:  JSON-LD JobPosting (title, description, datePosted, validThrough, jobLocation,
      hiringOrganization, employmentType, baseSalary, identifier).

Boards are on their customers' domains (jobs.acevo.org.uk, appointments.thetimes.com,
jobs.thesun.co.uk, ...; the vendor's ``example-sites.js`` lists ~40). They are recognised
by the platform's asset paths (``/_cachebusted/_resx/templates/{nnnnn}/``) + canonical URL.
robots.txt allows the job pages for ``*`` (it disallows admin/tracking paths, and some
boards name AI crawlers such as ClaudeBot, which is not our User-Agent).

Board identifier: the site host. Verified on jobs.acevo.org.uk (15 jobs) and
jobs.homeless.org.uk (55), 2026-11-08; the Times (~39,900) and Sun (~49,900) boards use
the same layout.
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class JobBoardComConnector(HostedBoardConnector):
    platform = "jobboardcom"
    page_fingerprint = re.compile(r"/_cachebusted/_resx/templates/\d+/|/_resx/common/js/")
    job_path = re.compile(r"/jobs/[^/]+/(?P<id>\d+-\d+)/?")

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}/sitemap.xml"]
