"""Niceboard job boards (generic sitemap + JSON-LD connector).

List: robots.txt -> /sitemap.xml (index) -> /sitemap-jobs.xml: every open job with
      ``<lastmod>``. Other children (base pages, companies, blog) are skipped.
Job:  /job/{id}-{slug}, JSON-LD JobPosting (validThrough, identifier, jobLocationType).
      The board API needs the owner's key, so public pages are used.

Board identifier: the board host (``jobs.nonprofittalent.com``, ``{board}.niceboard.co``).
Custom domains are recognised by Niceboard's CDN (cdn.niceboard.co) + canonical URL.
Verified on jobs.nonprofittalent.com and jobs.forcebrands.com (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class NiceboardConnector(HostedBoardConnector):
    platform = "niceboard"
    native_suffix = ".niceboard.co"
    page_fingerprint = re.compile(r"//cdn2?\.niceboard\.co/")
    child_sitemap = re.compile(r"/sitemap-jobs[^/]*\.xml$")
    job_path = re.compile(r"/job/(?P<id>\d+)-[^/]*/?")
