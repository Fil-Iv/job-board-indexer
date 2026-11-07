"""Phenom People career sites (generic sitemap + JSON-LD connector).

List: GET {site}/{country}/{lang}/sitemap.xml: one urlset per locale with every job
      (``<lastmod>``) plus content pages. Other locales (``/us/es``) list the same jobs
      and are not read.
Job:  {site}/{country}/{lang}/job/{id}/{slug}, JSON-LD JobPosting. Every page carries a
      hidden "the job you are trying to apply for has been filled" modal, so closed notices
      are only taken from the default patterns (which do not match that text).

Board identifier: ``{host}/{country}/{lang}`` (``careers.butterball.com/us/en``). Phenom sites
are custom domains, recognised by Phenom's CDN (*.phenompeople.com) + canonical URL.
Verified on careers.butterball.com/us/en and careers.shutterstock.com/us/en (2026-11-07).
"""

import re
from urllib.parse import urlsplit

from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register

_LOCALE = re.compile(r"/(?P<cc>[a-z]{2,6})/(?P<lang>[a-z]{2})(?=/|$)")
_BOARD = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}/[a-z]{2,6}/[a-z]{2}$")


@register
class PhenomConnector(GenericJsonLdConnector):
    platform = "phenom"
    page_fingerprint = re.compile(r"//(?:cdn|assets|pp-cdn|cdn-prod-static)\.phenompeople\.com/")

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None  # custom domains only: see detect_in_html

    @classmethod
    def board_from_page_url(cls, url: str) -> str | None:
        parts = urlsplit(url)
        match = _LOCALE.match(parts.path.lower())
        host = (parts.hostname or "").lower()
        return f"{host}/{match['cc']}/{match['lang']}" if match and host else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}"

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}/sitemap.xml"]

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        host, _, locale = board_identifier.partition("/")
        if (parts.hostname or "").lower() != host:
            return None
        # ids are requisition numbers (R0003176, DISTR004109); the sitemap also contains a
        # template entry ".../job/${jobSeqNo}/..." that must not count as a job.
        pattern = rf"/{re.escape(locale)}/job/([A-Za-z0-9_-]+)(?:/[^/]*)?/?"
        match = re.fullmatch(pattern, parts.path, re.I)
        return match.group(1) if match else None
