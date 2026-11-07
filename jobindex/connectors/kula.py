"""Kula career sites (``careers.kula.ai/{company}``; generic sitemap + JSON-LD connector).

List: GET https://careers.kula.ai/{company}/sitemap.xml: the board page plus every open job
      ``/{company}/{job id}-{slug}`` with ``<lastmod>`` (the board page itself renders its
      job list client-side). There is no site-wide sitemap (``/sitemap.xml``: 404).
Job:  JSON-LD JobPosting (title, description, datePosted, employmentType, baseSalary,
      hiringOrganization, jobLocation, identifier = job id).
      robots.txt disallows ``/api/`` and ``/*/portal`` only.

Board identifier: the company slug (``archetype-ai``).
Verified on archetype-ai and deepscribe (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register
from jobindex.normalize import split_url

HOST = "careers.kula.ai"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_RESERVED = {"api", "_next", "static", "assets", "robots.txt", "sitemap.xml"}
_JOB = re.compile(r"^(?P<id>\d+)(?:-[^/]*)?$")


@register
class KulaConnector(GenericJsonLdConnector):
    platform = "kula"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        slug = segments[0].lower() if host == HOST and segments else ""
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
        if host != HOST or len(segments) != 2 or segments[0].lower() != board_identifier:
            return None
        match = _JOB.match(segments[1])
        return match["id"] if match else None
