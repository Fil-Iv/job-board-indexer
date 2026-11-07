"""ApplicantPro career sites (generic sitemap + JSON-LD connector).

List: GET https://{tenant}.applicantpro.com/job_site_map.xml — the tenant's open jobs with
      ``<lastmod>`` (as www.applicantpro.com/openings/{tenant}/jobs/{id}). The tenant's
      /sitemap.xml lists ~1000 search/browse pages and is not used; robots.txt points at
      feeds.applicantpro.com/site_map_index.xml, an index of every tenant's job sitemap.
Job:  https://{tenant}.applicantpro.com/jobs/{id}.html (fetched directly: the openings URL
      redirects there), JSON-LD JobPosting with structured salary. ``validThrough`` is
      malformed ("2026-12-03 00:00:00T23:59"); its date is used.

Board identifier: the tenant subdomain (``mwra``).
Verified on mwra and commquest (2026-11-07). iSolved Hire runs the same software
(jobindex.connectors.isolvedhire).
"""

import re
from typing import ClassVar
from urllib.parse import urlsplit

from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register

_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NOT_TENANTS = frozenset({"www", "feeds", "api", "app", "admin", "help", "support", "static"})


@register
class ApplicantProConnector(GenericJsonLdConnector):
    platform = "applicantpro"
    domain: ClassVar[str] = "applicantpro.com"

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        host = (parts.hostname or "").lower()
        if host == f"www.{cls.domain}":  # www.applicantpro.com/openings/{tenant}/jobs/...
            match = re.match(r"/openings/([^/]+)/", parts.path)
            tenant = match.group(1).lower() if match else ""
        else:
            tenant = host.removesuffix(f".{cls.domain}") if host.endswith(f".{cls.domain}") else ""
        if not _TENANT.match(tenant) or tenant in _NOT_TENANTS:
            return None
        return tenant

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}.{cls.domain}/jobs/"

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}.{self.domain}/job_site_map.xml"]

    def job_id(self, url: str, board_identifier: str) -> str | None:
        if self.detect(url) != board_identifier:
            return None
        path = urlsplit(url).path
        match = re.fullmatch(r"/jobs/(\d+)(?:-[^/]*)?\.html", path) or re.fullmatch(
            r"/openings/[^/]+/jobs/(\d+)(?:/.*)?", path
        )
        return match.group(1) if match else None

    def page_url(self, url: str, external_id: str, board_identifier: str) -> str:
        return f"https://{board_identifier}.{self.domain}/jobs/{external_id}.html"
