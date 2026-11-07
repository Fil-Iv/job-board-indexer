"""PageUp career sites (``careers.pageuppeople.com/{id}/{portal}/{lang}``; RSS feed).

List: GET https://careers.pageuppeople.com/{id}/{portal}/{lang}/rss
      RSS 2.0 (namespaces job = http://pageuppeople.com/, a10 = Atom), one <item> per open
      job, with descriptions: guid/link ``.../job/{refNo}``, title, pubDate,
      ``a10:updated``, ``job:refNo``, ``job:description`` (HTML), ``job:location``
      ("Orlando", "USA|Massachusetts"), ``job:workType``, ``job:category`` (repeated),
      ``job:closingDate`` (may be empty), ``job:applyLink``.
      Complete: it matched the listing (20 shown + "More Jobs 124" = 144 items for
      851/ci/en-us; 20 + 33 = 53 for 889/usw/en, 2026-11-07); the listing itself pages
      20 at a time. One request per crawl. Unknown sites answer 404.
      robots.txt disallows only test/staging portals (``/uat``, ``/ci`` at the root, ...).

A posting whose ``job:closingDate`` has passed is returned with ``expires_at`` set, so the
crawler stores it closed.

Board identifier: ``{id}/{portal}/{lang}`` (``851/ci/en-us``).

URL formats recognised:
    careers.pageuppeople.com/{id}/{portal}/{lang}[/listing|/job/{refNo}|/rss]
Custom-domain career pages: links/iframes to the above (generic HTML detection).

Verified on Universal Orlando (851/ci/en-us) and Flight Centre (889/usw/en), 2026-11-07.
"""

import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException, ElementTree

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.normalize import normalize_employment_type, parse_datetime, split_url

HOST = "careers.pageuppeople.com"
_NS = {"job": "http://pageuppeople.com/", "a10": "http://www.w3.org/2005/Atom"}
_ID = re.compile(r"^\d+$")
_PORTAL = re.compile(r"^[a-z0-9]{1,20}$")
_LANG = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")


def _text(item: Element, tag: str) -> str | None:
    node = item.find(tag, _NS)
    text = (node.text or "").strip() if node is not None else ""
    return text or None


def _rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value.replace(" Z", " GMT"))
    except (TypeError, ValueError):
        return None


@register
class PageUpConnector(BaseConnector):
    platform = "pageup"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        lowered = [s.lower() for s in segments]
        if host != HOST or len(lowered) < 3:
            return None
        site, portal, lang = lowered[:3]
        if not (_ID.match(site) and _PORTAL.match(portal) and _LANG.match(lang)):
            return None
        return f"{site}/{portal}/{lang}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/{board_identifier}/listing/"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = f"https://{HOST}/{board_identifier}/rss"
        response = await self.http.get(url)
        try:
            root = ElementTree.fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            raise ConnectorError(f"pageup: {url} is not valid XML: {exc}") from exc
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise ConnectorError(f"pageup: {url} is not an RSS feed")
        jobs: dict[str, RawJob] = {}
        for item in channel.findall("item"):
            job = self._map(item)
            jobs[job.external_id] = job
        return list(jobs.values())

    @staticmethod
    def _map(item: Element) -> RawJob:
        link = _text(item, "link") or _text(item, "guid")
        ref = _text(item, "job:refNo") or (link.rstrip("/").rsplit("/", 1)[-1] if link else None)
        if not ref:
            raise ConnectorError("pageup: item without job:refNo or link")
        location = _text(item, "job:location")
        return RawJob(
            external_id=ref,
            title=_text(item, "title") or "",
            description_html=_text(item, "job:description") or _text(item, "description"),
            location_raw=", ".join(reversed(location.split("|"))) if location else None,
            employment_type=normalize_employment_type(_text(item, "job:workType")),
            posted_at=_rfc822(_text(item, "pubDate")) or parse_datetime(_text(item, "a10:updated")),
            source_url=link,
            apply_url=_text(item, "job:applyLink") or link,
            expires_at=_rfc822(_text(item, "job:closingDate")),
        )
