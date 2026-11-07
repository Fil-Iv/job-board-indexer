"""Deel ATS job boards (``jobs.deel.com/{org}``; generic sitemap + JSON-LD connector).

List: GET https://jobs.deel.com/{org}/sitemap.xml: the board page plus every open job
      ``/{org}/job-details/{uuid}/overview`` with ``<lastmod>``. robots.txt points at these
      per-org sitemaps in a comment (the ``Sitemap:`` directive has no wildcards). It
      matched the board's JSON-LD ItemList (guardare: 3/3, 2026-11-07).
Job:  JSON-LD JobPosting (title, description, datePosted, employmentType,
      hiringOrganization, jobLocation, identifier = the job uuid).
      robots.txt disallows ``/api/`` and ``/*/job-details/*/application`` only.

Board identifier: the org slug (``guardare``). ``jobs.deel.com/job-boards/{org}``
redirects to ``jobs.deel.com/{org}``.
Verified on guardare and apmc-careers (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register
from jobindex.normalize import split_url

HOST = "jobs.deel.com"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_RESERVED = {"api", "_next", "job-boards", "static", "assets", "robots.txt", "sitemap.xml"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@register
class DeelConnector(GenericJsonLdConnector):
    platform = "deel"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host != HOST or not lowered:
            return None
        slug = lowered[1] if lowered[0] == "job-boards" and len(lowered) > 1 else lowered[0]
        if slug in _RESERVED or not _SLUG.match(slug):
            return None
        return slug

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/{board_identifier}"

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{HOST}/{board_identifier}/sitemap.xml"]

    def job_id(self, url: str, board_identifier: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host != HOST or len(lowered) < 3 or lowered[0] != board_identifier:
            return None
        if lowered[1] != "job-details" or not _UUID.match(lowered[2]):
            return None
        return lowered[2]
