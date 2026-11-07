"""Hireology career sites (generic sitemap + JSON-LD connector).

List: GET https://careers.hireology.com/sitemap_index.xml -> sitemaps/sitemap-{n}.xml: one
      sitemap for *all* tenants (~57k URLs, ~8 MB, with ``<lastmod>``); the tenant's jobs
      are the /{slug}/{id}/description URLs.
Job:  JSON-LD JobPosting. ``validThrough`` is ignored: it is date posted + 30 days and in the
      past on jobs that are still open (San Diego Padres, 4 of 6 sampled, 2026-11-07).

Board identifier: the tenant slug (``sandiegopadres``).
Verified on sandiegopadres and nextpoint (2026-11-07). The career site also calls
api.hireology.com/v2; a per-tenant JSON endpoint there would avoid the shared sitemap and
the per-job pages (not investigated yet).
"""

import re
from urllib.parse import urlsplit

from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register

HOST = "careers.hireology.com"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_NOT_SLUGS = frozenset({"sitemaps", "sitemap_index.xml", "sitemap.xml", "robots.txt", "assets"})


@register
class HireologyConnector(GenericJsonLdConnector):
    platform = "hireology"
    trust_valid_through = False

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        if (parts.hostname or "").lower() != HOST:
            return None
        slug = (parts.path.strip("/").split("/") or [""])[0].lower()
        return slug if _SLUG.match(slug) and slug not in _NOT_SLUGS else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/{board_identifier}"

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{HOST}/sitemap_index.xml"]

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != HOST:
            return None
        match = re.fullmatch(r"/([^/]+)/(\d+)/description/?", parts.path)
        if not match or match.group(1).lower() != board_identifier:
            return None
        return match.group(2)
