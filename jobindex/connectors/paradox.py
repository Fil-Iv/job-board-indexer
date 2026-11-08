"""Paradox career sites (employer domains such as ``careers.fedex.com``, ``careers.marriott.com``).

Paradox ("Olivia", acquired by Workday in 2025) hosts employers' career sites on their own
domains (CNAME to ``{brand}.sites.paradox.ai``). The job list is not in the page (it is
rendered by JavaScript), but every site publishes a complete sitemap:

List: robots.txt -> ``Sitemap: /sitemap.xml`` (or ``/sitemap.xml`` when undeclared): an
      index of ``sitemap-{brand id}-{locale}.xml`` files (one per brand x language) plus
      ``sitemap-content.xml`` (content pages, ignored by ``job_id``). Job pages are
      ``/{slug}/job/{id}``; translations repeat the same job as ``/{locale}/{slug}/job/{id}``
      (FedEx) or ``/{locale}/{slug}/job/{id}__{locale}`` (Marriott). Jobs are keyed by the
      base id and the default-language page is kept when a job is listed in several.
Job:  JSON-LD JobPosting (title, description, datePosted, hiringOrganization,
      jobLocation, ...).

Board identifier: the employer's site host (``{brand}.sites.paradox.ai`` serves the same
site, but the sitemaps list the employer's domain, so URLs are never detected from the host
alone). Pages are recognised by their assets
(``cdn.sites.{brand}.paradox.ai/{brand}-prod-careersites/...``, or
``cdn.sites.paradox.ai/paradox-prod-careersites/...`` on Marriott) + canonical URL; the
``*.sites.paradox.ai`` DNS is a wildcard, so host names alone prove nothing.
Verified on careers.fedex.com (~4,700 job URLs) and careers.marriott.com, 2026-11-08.
Not every Paradox customer fits: jobs.mchire.com (McDonald's franchisees) has ~1,000
sitemaps, over MAX_SITEMAP_FILES.
"""

import re
from typing import ClassVar
from urllib.parse import urlsplit

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register

_LOCALE = r"[a-z]{2}(?:-[a-z]{2,4})?"


@register
class ParadoxConnector(HostedBoardConnector):
    platform = "paradox"
    page_fingerprint = re.compile(
        r"cdn\.sites\.(?:[a-z0-9-]+\.)?paradox\.ai/[a-z0-9-]+-prod-careersites/"
    )
    job_path: ClassVar[re.Pattern[str]] = re.compile(
        rf"/(?:{_LOCALE}/)?[^/]+/job/(?P<id>[A-Za-z0-9-]+?)(?:__{_LOCALE})?/?"
    )
    _LOCALE_PREFIX = re.compile(rf"^/{_LOCALE}/[^/]+/job/")

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        listed = await super().listed_pages(board_identifier)
        default_language = {
            job_id
            for url, _ in listed
            if (job_id := self.job_id(url, board_identifier))
            and not self._LOCALE_PREFIX.match(urlsplit(url).path)
        }
        return [
            (url, lastmod)
            for url, lastmod in listed
            if not (
                self._LOCALE_PREFIX.match(urlsplit(url).path)
                and self.job_id(url, board_identifier) in default_language
            )
        ]
