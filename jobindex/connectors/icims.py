"""iCIMS career sites (generic sitemap + JSON-LD connector).

List: robots.txt -> /sitemap.xml: one urlset with every open job (``<lastmod>``) plus the
      search page.
Job:  /jobs/{id}/{slug}/job, JSON-LD JobPosting. iCIMS fills ``hiringOrganization`` with
      "UNAVAILABLE"; that is dropped (the source's company name is used instead).

Board identifier: the career-site host (``careers-eastwestbank.icims.com``; tenants use
various prefixes). Custom domains are recognised by iCIMS' CDN (cdnNN.icims.com).
Verified on careers-eastwestbank.icims.com and careers-emcorgroup.icims.com (2026-11-07).
"""

import re
from dataclasses import replace
from typing import Any

from jobindex.connectors.base import RawJob
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register

_PLACEHOLDERS = frozenset({"unavailable", "n/a", "na", "none"})


@register
class ICIMSConnector(HostedBoardConnector):
    platform = "icims"
    native_suffix = ".icims.com"
    page_fingerprint = re.compile(r"//cdn\d*\.icims\.com/")
    job_path = re.compile(r"/jobs/(?P<id>\d+)/[^/]*/job/?")

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        if job.company and job.company.strip().lower() in _PLACEHOLDERS:
            return replace(job, company=None)
        return job
