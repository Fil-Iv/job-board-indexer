"""Recooty career pages (generic sitemap + JSON-LD connector).

List: GET https://jobs.recooty.com/sitemap.xml: one sitemap for *all* tenants (~2300 URLs,
      no ``<lastmod>``, so job pages are re-fetched every GENERIC_REFRESH_DAYS); a
      tenant's jobs are /{slug}/{title}-{job code} (job code: 2 letters + number, "di101").
      It matched the tenant's paged listing (Discovery Care Group: 28 vs 3 pages of 10,
      2026-11-07).
Job:  JSON-LD JobPosting (no validThrough).

Board identifier: the tenant slug (``discovery-care-group``).
Verified on discovery-care-group and gibbons-group (2026-11-07). Recooty has no public API.
"""

import re
from urllib.parse import urlsplit

from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register

HOST = "jobs.recooty.com"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_JOB = re.compile(r"/(?P<slug>[^/]+)/[^/]*-(?P<id>[a-z]{2}\d+)/?")


@register
class RecootyConnector(GenericJsonLdConnector):
    platform = "recooty"

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        if (parts.hostname or "").lower() != HOST:
            return None
        slug = parts.path.strip("/").split("/")[0].lower()
        return slug if _SLUG.match(slug) and "." not in slug else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/{board_identifier}"

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{HOST}/sitemap.xml"]

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        match = _JOB.fullmatch(parts.path)
        if (parts.hostname or "").lower() != HOST or not match:
            return None
        return match["id"] if match["slug"].lower() == board_identifier else None
