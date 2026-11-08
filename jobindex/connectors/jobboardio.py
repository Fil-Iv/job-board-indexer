"""JobBoard.io job boards (generic sitemap + JSON-LD connector; custom domains only).

List: robots.txt -> /sitemap.xml (index) -> /sitemaps/jobs/job_sitemap_details.xml
      ?finish_id=..&start_id=.. (job id ranges, 2,500 URLs each) with ``<lastmod>``; page and
      taxonomy sitemaps are skipped. The child URLs are XML-escaped (``&amp;``), see
      ``jobindex.discovery``.
Job:  /jobs/{id}-{slug}: JSON-LD JobPosting (title, description, datePosted, validThrough,
      employmentType, baseSalary, hiringOrganization, jobLocation).
      robots.txt (identical on every board) disallows the trackers, ``/backfills/
      latest_jobs``, employer and search-filter URLs only.

Board identifier: the board host (``jobs.cityandstateny.com``). Boards live on customers'
domains; they are recognised by the theme stylesheet ``/assets/boards/{theme}-theme-
{hash}.css`` + the page's canonical URL.
Verified on jobs.cityandstateny.com and jobs.blooloop.com (2026-11-08); also
jobs.motionographer.com, jobs.tdwi.org, jobs.hireheroesusa.org.
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class JobBoardIoConnector(HostedBoardConnector):
    platform = "jobboardio"
    page_fingerprint = re.compile(r"/assets/boards/[a-z0-9-]+-theme-[0-9a-f]{16,}\.css")
    child_sitemap = re.compile(r"/sitemaps/jobs/")
    job_path = re.compile(r"/jobs/(?P<id>\d+)-[^/]*/?")
