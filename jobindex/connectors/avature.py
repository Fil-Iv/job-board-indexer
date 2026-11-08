"""Avature career portals (``{tenant}.avature.net/{portal}``: ``bloomberg.avature.net/careers``).

List: GET {host}/{portal}/sitemap_index.xml -> {portal}/sitemap.xml: every job page
      ``/{portal}/JobDetail/{slug}/{id}`` with ``<lastmod>`` (plus content pages). Checked
      against the total of the search page (GET {portal}/SearchJobs/?jobOffset=0,
      ``<div class="list-controls__text__legend" aria-label="323 results">``): fewer jobs
      in the sitemap than the board shows is an error; more is allowed (a job page that is
      no longer open is stored closed). The paged search itself is not used to list jobs:
      its order is not stable (Bloomberg, 2026-11-08: 9 of 323 jobs appeared on two pages
      and 9 on none), and ``/SearchJobs/feed/`` (RSS) always stops at 20 items.
Job:  {host}/{portal}/JobDetail/{slug}/{id}: no JSON-LD. The posting is the
      ``article__content__view__field`` blocks of Avature's standard template: labelled
      fields (``__label`` + ``__value``: "Location", "Posted Date", "Employment Type", ...),
      one-line unlabelled fields written as "Label: value" ("Work Location: Los Angeles,
      CA, USA") and long unlabelled rich-text blocks (the description). Tenants choose
      which fields they show, so every field is optional except title and description.
      The employer is the last part of the page ``<title>`` ("... - 46004 - Bloomberg").

robots.txt (per tenant) allows the portals and disallows only URLs with ``qtvc=``; some
tenants (deloitte.avature.net) disallow everything except the sitemap, so they fail with
``RobotsDisallowedError``.

Board identifier: ``{host}/{portal}`` (``bloomberg.avature.net/careers``).
Verified on bloomberg.avature.net/careers (323 jobs) and uclahealth.avature.net/careers
(739), 2026-11-08.
"""

import re
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register
from jobindex.normalize import normalize_employment_type, parse_datetime

SUFFIX = ".avature.net"
_NOT_TENANTS = {"www", "api", "cdn", "static", "help", "support"}
_PORTAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOTAL = re.compile(r"list-controls__text__legend\"[^>]*aria-label=\"([\d,.\s]+)results\"", re.I)
_JOB_PATH = re.compile(r"^/(?P<portal>[^/]+)/JobDetail/(?:[^/]+/)?(?P<id>\d+)/?$")
_INLINE_FIELD = re.compile(r"^(?P<label>[A-Za-z][A-Za-z /#&-]{1,40}):\s*(?P<value>.+)$", re.S)
#: Unlabelled blocks shorter than this are single values, not description.
DESCRIPTION_MIN_CHARS = 200


@register
class AvatureConnector(GenericJsonLdConnector):
    platform = "avature"

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        host = (parts.hostname or "").lower()
        segments = [s for s in parts.path.split("/") if s]
        tenant = host.removesuffix(SUFFIX)
        if not host.endswith(SUFFIX) or "." in tenant or tenant in _NOT_TENANTS or not segments:
            return None
        if not _PORTAL.match(segments[0]) or segments[0].lower() in ("portal", "sitemap.xml"):
            return None
        return f"{host}/{segments[0]}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}/SearchJobs"

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        host, _, portal = board_identifier.partition("/")
        match = _JOB_PATH.match(parts.path)
        if (parts.hostname or "").lower() != host or match is None or match["portal"] != portal:
            return None
        return match["id"]

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        # robots.txt declares one sitemap per portal (events, talent, ...): only this one.
        return [f"https://{board_identifier}/sitemap_index.xml"]

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        listed = await super().listed_pages(board_identifier)
        jobs = {job_id for url, _ in listed if (job_id := self.job_id(url, board_identifier))}
        total = await self.listed_total(board_identifier)
        if len(jobs) < total:
            raise ConnectorError(
                f"avature: sitemap lists {len(jobs)} jobs but the board says {total}"
            )
        return listed

    async def listed_total(self, board_identifier: str) -> int:
        """The job count the search page shows (``aria-label="323 results"``)."""
        url = f"{self.board_url(board_identifier)}/?jobOffset=0"
        match = _TOTAL.search(await self._get_text(url))
        if match is None:
            raise ConnectorError(f"avature: no result count on {url}; template changed?")
        return int(re.sub(r"\D", "", match.group(1)) or 0)

    def parse_page_html(self, html: str, stub: RawJob) -> RawJob | None:
        tree = LexborHTMLParser(html)
        nodes = tree.css(".article__content__view__field")
        title = _page_title(tree)
        if not nodes or not title:
            return None
        labelled: dict[str, str] = {}
        description: list[str] = []
        for node in nodes:
            label_node = node.css_first(".article__content__view__field__label")
            value_node = node.css_first(".article__content__view__field__value") or node
            text = " ".join(value_node.text(separator=" ").split())
            if not text or text == title:
                continue
            if label_node is not None and label_node.text(strip=True):
                label = label_node.text(strip=True).rstrip(":").strip().lower()
                labelled.setdefault(label, text)
            elif len(text) < DESCRIPTION_MIN_CHARS and (inline := _INLINE_FIELD.match(text)):
                labelled.setdefault(inline["label"].strip().lower(), inline["value"].strip())
            else:
                description.append(_inner_html(value_node))
        if not description:
            return None

        def first(*words: str) -> str | None:
            return next((v for k, v in labelled.items() if any(w in k for w in words)), None)

        return RawJob(
            external_id=stub.external_id,
            title=title,
            company=_company(tree),
            description_html="".join(description),
            location_raw=first("location"),
            employment_type=normalize_employment_type(first("employment type", "job type")),
            posted_at=_posted(first("posted", "posting date", "date published")),
            source_url=stub.source_url,
            apply_url=_apply_url(tree, stub.source_url or ""),
        )

    def adjust_page(self, job: RawJob, html: str) -> RawJob:
        return job if job.company else replace(job, company=_company(LexborHTMLParser(html)))


def _title_parts(tree: LexborHTMLParser) -> list[str]:
    node = tree.css_first("title")
    text = " ".join(node.text().split()) if node else ""
    return [p.strip() for p in text.split(" - ") if p.strip()]


def _page_title(tree: LexborHTMLParser) -> str:
    og = tree.css_first('meta[property="og:title"]')
    value = (og.attributes.get("content") or "").strip() if og is not None else ""
    if value:
        return " ".join(value.split())
    parts = _title_parts(tree)
    return parts[0] if parts else ""


def _company(tree: LexborHTMLParser) -> str | None:
    """``<title>Job title - - 46004 - Bloomberg</title>``: the last part, unless it is the
    job number."""
    parts = _title_parts(tree)
    if len(parts) >= 2 and not parts[-1].isdigit():
        return parts[-1]
    return None


def _inner_html(node: LexborNode) -> str:
    return "".join(child.html or "" for child in node.iter(include_text=True))


def _apply_url(tree: LexborHTMLParser, source_url: str) -> str | None:
    for link in tree.css("a[href]"):
        href = link.attributes.get("href") or ""
        if "/ApplicationMethods" in href or "/Register?" in href or "/Login?" in href:
            return unescape(href)
    return source_url or None


def _posted(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%d-%b-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return parse_datetime(value)
