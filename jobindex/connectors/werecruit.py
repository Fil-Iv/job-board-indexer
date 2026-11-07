"""WeRecruit career sites (generic sitemap + JSON-LD connector).

List: GET https://careers.werecruit.io/sitemap.xml: one sitemap for *all* tenants (~23k
      URLs, 3.5 MB, with ``<lastmod>``); a tenant's jobs are
      /{lang}/{slug}/offres|offers/{title}-{6 hex chars}.
Job:  JSON-LD JobPosting (script type written as ``application/ld&#x2B;json``; no
      validThrough: closed offers drop out of the sitemap).

Board identifier: the tenant slug (``dessange``).
Verified on dessange and declic-immo (2026-11-07).
"""

import re
from urllib.parse import urlsplit

from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register

HOST = "careers.werecruit.io"
_BOARD = re.compile(r"/[a-z]{2}/(?P<slug>[a-z0-9][a-z0-9-]*)(?:/|$)")
_JOB = re.compile(
    r"/[a-z]{2}/(?P<slug>[^/]+)/(?:offres|offers|angebote|ofertas)/[^/]*-(?P<id>[0-9a-f]{6})/?"
)


@register
class WeRecruitConnector(GenericJsonLdConnector):
    platform = "werecruit"

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        if (parts.hostname or "").lower() != HOST:
            return None
        match = _BOARD.match(parts.path.lower())
        return match["slug"] if match else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/fr/{board_identifier}"

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{HOST}/sitemap.xml"]

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        match = _JOB.fullmatch(parts.path)
        if (parts.hostname or "").lower() != HOST or not match:
            return None
        return match["id"] if match["slug"].lower() == board_identifier else None
