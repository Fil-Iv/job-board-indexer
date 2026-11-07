"""SAP SuccessFactors Recruiting Marketing (RMK) career sites (generic sitemap + JSON-LD).

List: GET {site}/sitemap.xml: every open job as /job/{slug}/{id}/ with ``<lastmod>``.
Job:  microdata JobPosting; dates are Java ``Date.toString()`` values
      ("Mon Oct 12 00:00:00 UTC 2026"); hand-typed addressCountry fragments ("No") are
      dropped.

Board identifier: the career-site host (``careers.yash.com``). RMK sites are custom
domains, recognised by the RMK CDN (rmkcdn.successfactors.com) + canonical URL. The classic
career{n}.successfactors.com/career?company= page is JS-only and not covered.
Verified on careers.yash.com, careers.airindia.com and careers.nttdata-solutions.com
(2026-11-07).
"""

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from jobindex.connectors.base import RawJob
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register
from jobindex.normalize import parse_datetime


@register
class SuccessFactorsConnector(HostedBoardConnector):
    platform = "successfactors"
    page_fingerprint = re.compile(r"//rmkcdn\.successfactors\.com/")
    job_path = re.compile(r"/job/[^/]+/(?P<id>\d+(?:-[a-z]{2}_[A-Z]{2})?)/?")

    def parse_date(self, value: str) -> datetime | None:
        try:  # "Mon Oct 12 00:00:00 UTC 2026"
            return datetime.strptime(value, "%a %b %d %H:%M:%S UTC %Y").replace(tzinfo=UTC)
        except ValueError:
            return parse_datetime(value)

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        # Tenants fill addressCountry by hand: keep ISO codes ("IN") and names, drop
        # fragments such as "No" (Air India), which would read as Norway.
        country = job.country
        if country and not (re.fullmatch(r"[A-Z]{2,3}", country) or len(country) > 3):
            parts = [p.strip() for p in (job.location_raw or "").split(",")]
            location = ", ".join(parts[:-1]) if parts and parts[-1] == country else None
            return replace(job, country=None, location_raw=location or job.location_raw)
        return job
