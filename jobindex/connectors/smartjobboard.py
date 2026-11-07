"""SmartJobBoard job boards (generic sitemap + JSON-LD connector).

List: /sitemap.xml (one urlset; job URLs carry ``<lastmod>``, browse pages do not).
Job:  /job/{numeric id}/{slug}/, JSON-LD JobPosting (validThrough, baseSalary).
      /company/{id}/..., /jobs/{slug}/ (searches) are skipped.

Board identifier: the board host (``jobs.alanet.org``, ``{board}.mysmartjobboard.com``).
Custom domains are recognised by SmartJobBoard's CDN (cdn.smartjobboard.com).
Verified on jobsboard.edsurge.com and jobs.alanet.org (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class SmartJobBoardConnector(HostedBoardConnector):
    platform = "smartjobboard"
    native_suffix = ".mysmartjobboard.com"
    page_fingerprint = re.compile(r"//cdn\.smartjobboard\.com/")
    job_path = re.compile(r"/job/(?P<id>\d+)/[^/]*/?")
