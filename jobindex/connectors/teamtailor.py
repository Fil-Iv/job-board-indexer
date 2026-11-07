"""Teamtailor career sites (public RSS feed of every career site).

List: GET https://{career-site-host}/jobs.rss
      -> RSS 2.0 <item><title/><description>HTML</description><pubDate/><link/>
         <remoteStatus/><guid/><tt:locations><tt:location><tt:name/><tt:city/><tt:country/>
         ...</tt:location></tt:locations><tt:department/></item>
      (``tt`` = https://teamtailor.com/locations). Every published job with its full
      description in one response. The JSON API needs a customer token; we do not use it.

Board identifier: the career-site host — ``{company}.teamtailor.com`` or the company's own
domain (e.g. ``jobs.lunar.app``). Custom-domain sites never mention their
``*.teamtailor.com`` name (verified on jobs.lunar.app, 2026-11-07), so the host itself is the
identifier; it serves ``/jobs.rss`` like the subdomain does.

Completeness: on every board checked the feed listed exactly the jobs of the paged HTML
listing (Oatly 25/25, Lunar 16/16; 2026-11-07), but no board above 25 jobs could be checked.
For feeds with ``CROSSCHECK_FROM`` items or more we therefore also page through
``/jobs?page=n`` and refuse the result if the HTML lists a job the feed lacks.

``remoteStatus``: fully | hybrid | onsite | none ("none" = not specified).

URL formats recognised:
    {company}.teamtailor.com[/jobs[/{id}-{slug}[/applications/new]]]  .../jobs.rss
Custom domains: recognised from page HTML (Teamtailor CDN assets + the page's jobs.rss link).
"""

import re
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException, ElementTree

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import split_url

TT = "{https://teamtailor.com/locations}"
CROSSCHECK_FROM = 50
MAX_HTML_PAGES = 100
_SUBDOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_HOST = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
_RESERVED = {"www", "app", "api", "support", "docs", "status", "partner", "career", "blog"}
_REMOTE = {"fully": RemoteType.REMOTE, "hybrid": RemoteType.HYBRID, "onsite": RemoteType.ONSITE}
_JOB_ID = re.compile(r"/jobs/(\d+)-")
_RSS_LINK = re.compile(
    r"<link[^>]+type=\"application/rss\+xml\"[^>]+href=\"https://(?P<host>[A-Za-z0-9.-]+)/jobs\.rss\""
)


def _text(element: Element | None, path: str) -> str | None:
    found = element.find(path) if element is not None else None
    value = (found.text or "").strip() if found is not None else ""
    return value or None


def _rfc822(value: str | None) -> Any:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


@register
class TeamtailorConnector(BaseConnector):
    platform = "teamtailor"

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        if not host.endswith(".teamtailor.com"):
            return None
        sub = host.removesuffix(".teamtailor.com")
        if "." in sub or sub in _RESERVED or not _SUBDOMAIN.match(sub):
            return None
        return host

    @classmethod
    def detect_in_html(cls, html: str) -> dict[str, int]:
        counts = super().detect_in_html(html)
        if "teamtailor-cdn.com" in html:  # a Teamtailor career site, possibly a custom domain
            for match in _RSS_LINK.finditer(html):
                host = match["host"].lower()
                counts[host] = counts.get(host, 0) + 1
        return counts

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}/jobs"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        if not _HOST.match(board_identifier):
            raise ConnectorError(f"teamtailor: bad board identifier {board_identifier!r}")
        response = await self.http.get(f"https://{board_identifier}/jobs.rss")
        try:
            root = ElementTree.fromstring(response.content)
        except (ParseError, DefusedXmlException) as exc:
            raise ConnectorError(f"teamtailor: feed is not valid/safe XML ({exc!r})") from exc
        channel = root.find("channel")
        if root.tag != "rss" or channel is None:
            raise ConnectorError("teamtailor: response is not an RSS feed")
        jobs = [self._map(item) for item in channel.findall("item")]
        if len(jobs) >= CROSSCHECK_FROM:
            await self._crosscheck(board_identifier, jobs)
        return jobs

    async def _crosscheck(self, board_identifier: str, jobs: list[RawJob]) -> None:
        """Refuse the feed if the paged HTML listing has a job the feed lacks."""
        in_feed = {m.group(1) for j in jobs if (m := _JOB_ID.search(j.source_url or ""))}
        listed: set[str] = set()
        for page in range(1, MAX_HTML_PAGES + 1):
            response = await self.http.get(f"https://{board_identifier}/jobs?page={page}")
            found = set(_JOB_ID.findall(response.text))
            if not found - listed:
                break
            listed |= found
        missing = listed - in_feed
        if missing:
            raise ConnectorError(
                f"teamtailor: feed lacks {len(missing)} of {len(listed)} jobs listed on the "
                "career site; refusing a partial result"
            )

    @staticmethod
    def _map(item: Element) -> RawJob:
        link = _text(item, "link")
        guid = _text(item, "guid")
        match = _JOB_ID.search(urlsplit(link).path) if link else None
        external_id = guid or (match.group(1) if match else None)
        if not external_id:
            raise ConnectorError("teamtailor: item without guid or job link")
        locations = item.findall(f"./{TT}locations/{TT}location")
        names = [_text(loc, f"{TT}name") for loc in locations]
        location_raw = "; ".join(dict.fromkeys(n for n in names if n)) or None
        single = locations[0] if len(locations) == 1 else None
        return RawJob(
            external_id=external_id,
            title=_text(item, "title") or "",
            description_html=_text(item, "description"),
            location_raw=location_raw,
            city=_text(single, f"{TT}city"),
            country=_text(single, f"{TT}country"),
            remote_type=_REMOTE.get(_text(item, "remoteStatus") or "", RemoteType.UNKNOWN),
            posted_at=_rfc822(_text(item, "pubDate")),
            source_url=link,
            apply_url=f"{link}/applications/new" if link else None,
        )
