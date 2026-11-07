"""Jobboardly job boards (generic sitemap + JSON-LD connector).

List: /sitemap.xml (one urlset with ``<lastmod>``; mixes home/listing pages and jobs).
Job:  /jobs/{slug}-{8 hex chars}, JSON-LD JobPosting (validThrough, identifier).
      Listing pages such as /jobs/contract have no hex suffix and are skipped.

Board identifier: the board host (``www.motorsport.jobs``, ``{board}.jobboardly.com``).
Custom domains are recognised by Jobboardly's CDN (cdn/assets.jobboardly.com).
Verified on www.motorsport.jobs and tradiejobs.com.au (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class JobboardlyConnector(HostedBoardConnector):
    platform = "jobboardly"
    native_suffix = ".jobboardly.com"
    page_fingerprint = re.compile(r"//(?:cdn|assets)\.jobboardly\.com/")
    job_path = re.compile(r"/jobs/[^/]*-(?P<id>[0-9a-f]{8})/?")
