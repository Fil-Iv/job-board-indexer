"""JBoard.io job boards (generic sitemap + JSON-LD connector).

List: robots.txt -> /sitemap.xml (index) -> /sitemap-jobs-{n}.xml. No ``<lastmod>``, so
      job pages are re-fetched every GENERIC_REFRESH_DAYS (staggered).
Job:  /jobs/{numeric id}-{slug}, JSON-LD JobPosting. /jobs/full-time, /jobs/in-{place}
      etc. are listing pages (no numeric id) and are skipped.

Board identifier: the board host (``nuclearjobscanada.ca``, ``{board}.jboard.io``).
Custom domains are recognised by the page config (admin_url app.jboard.io) + canonical URL.
Verified on nuclearjobscanada.ca and couriergigs.com (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class JBoardConnector(HostedBoardConnector):
    platform = "jboard"
    detail_version = 1  # 2026-11-10: "City, Region, Country" addressLocality split
    native_suffix = ".jboard.io"
    page_fingerprint = re.compile(r"app\.jboard\.io")
    child_sitemap = re.compile(r"/sitemap-jobs-\d+\.xml$")
    job_path = re.compile(r"/jobs/(?P<id>\d{5,})-[^/]*/?")
