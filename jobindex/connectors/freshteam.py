"""Freshteam (Freshworks) career sites (``{company}.freshteam.com/jobs``; HTML).

robots.txt on every tenant (same file, checked 2026-11-11)::

    User-agent: *
    Allow: /jobs
    Disallow: /

so the career pages are allowed and everything else (incl. the jobs widget JSON under
``/hire/widgets/``) is not. There is no JSON-LD and no sitemap.

List: GET https://{company}.freshteam.com/jobs -> server-rendered HTML with every open
      job (filters run client-side; no pagination). Each job is a card (an element
      with class ``heading``) linking ``/jobs/{id}/{slug}`` with the title (``.job-title``)
      and ``.location-info`` = "{location}<br/>{employment type}". The page's
      ``og:description`` starts with the count ("#95 Jobs - Beijing|...") and the
      number of distinct job links must match it. Tenants customise the Liquid template
      (renous groups cards by department, crimsoniteam uses the default list), so
      parsing relies on these shared class names only.
Job:  GET https://{company}.freshteam.com/jobs/{id}/{slug} -> ``h1`` title,
      ``.job-details-content`` description (a template bug on some tenants appends
      "Liquid error: ..." there; removed), ``og:description`` "Posted by : {company} |".
      Closed jobs answer 404.

A company that does not exist (or left Freshteam) gets a 200 "We couldn't find ..."
page, which is refused as an error, never read as an empty board.

Board identifier: the subdomain, e.g. ``renous``. Verified on renous (90 jobs) and
crimsoniteam (95), 2026-11-11.
"""

import logging
import re
from typing import ClassVar
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, split_url

log = logging.getLogger(__name__)

_SUFFIX = ".freshteam.com"
_COMPANY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RESERVED = {"www", "app", "api", "assets", "support", "help", "blog", "status", "cdn"}
_JOB_PATH = re.compile(r"^/jobs/(?P<id>[A-Za-z0-9_-]{6,})(?:/[^/]*)?/?$")
_COUNT = re.compile(r"^\s*#\s*(?P<n>\d+)\s+Jobs?\b", re.I)
_POSTED_BY = re.compile(r"Posted by\s*:\s*(?P<name>[^|]+?)\s*(?:\||$)")
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
#: Marks a real career page (both templates); the "unknown company" page has none.
_LIST_MARKERS = ("job-role-list", 'data-portal-id="jobs_list"')
_UNKNOWN_COMPANY = "invalid-domain-wrapper"


def _meta(tree: LexborHTMLParser, prop: str) -> str:
    node = tree.css_first(f'meta[property="{prop}"]')
    return (node.attributes.get("content") or "").strip() if node else ""


def _text(node: LexborNode | None) -> str:
    return " ".join(node.text(separator=" ").split()) if node else ""


def _card(link: LexborNode) -> LexborNode:
    """The job card around a job link: the closest element (incl. itself) with class
    ``heading``; the link itself if there is none."""
    node: LexborNode | None = link
    while node is not None and node.tag != "body":
        if "heading" in (node.attributes.get("class") or "").split():
            return node
        node = node.parent
    return link


def _location_and_type(card: LexborNode) -> tuple[str | None, str | None]:
    """``.location-info`` = "Mumbai, Maharashtra<br/>Full Time" (location may be empty)."""
    info = card.css_first(".location-info")
    if info is None:
        return None, None
    parts = [" ".join(_TAG.sub(" ", p).split()) for p in _BR.split(info.inner_html or "")]
    parts = [p.replace("&nbsp;", " ").strip() for p in parts]
    if len(parts) == 1:
        return parts[0] or None, None
    return parts[0] or None, parts[-1] or None


@register
class FreshteamConnector(BaseConnector):
    platform = "freshteam"
    needs_detail = True
    detail_fields: ClassVar[tuple[str, ...]] = ("description_html", "description_text", "company")

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, _, _ = split_url(url)
        if not host.endswith(_SUFFIX):
            return None
        company = host.removesuffix(_SUFFIX)
        if company in _RESERVED or not _COMPANY.match(company):
            return None
        return company

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}{_SUFFIX}/jobs"

    def max_job_drop(self) -> float | None:
        return get_settings().generic_max_job_drop

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = self.board_url(board_identifier)
        response = await self.http.get(url)
        html = response.text
        if _UNKNOWN_COMPANY in html:
            raise ConnectorError(f"freshteam: {board_identifier} is not a Freshteam company")
        if not any(marker in html for marker in _LIST_MARKERS):
            raise ConnectorError(f"freshteam: {url} has no job list (template changed?)")
        tree = LexborHTMLParser(html)
        host = f"{board_identifier}{_SUFFIX}"
        jobs: dict[str, RawJob] = {}
        for link in tree.css("a[href]"):
            target = urljoin(url, link.attributes.get("href") or "")
            parts = urlsplit(target)
            match = _JOB_PATH.match(parts.path)
            if (parts.hostname or "").lower() != host or match is None:
                continue
            job_id = match.group("id")
            if job_id in jobs:
                continue
            card = _card(link)
            title = _text(card.css_first(".job-title")) or _text(link)
            location, job_type = _location_and_type(card)
            remote = (card.attributes.get("data-portal-remote-location") or "").lower()
            jobs[job_id] = RawJob(
                external_id=job_id,
                title=title,
                location_raw=location,
                employment_type=normalize_employment_type(job_type),
                remote_type=RemoteType.REMOTE if remote == "true" else RemoteType.UNKNOWN,
                source_url=f"https://{host}{parts.path}",
                apply_url=f"https://{host}{parts.path}",
            )
        count = _COUNT.match(_meta(tree, "og:description"))
        if count is None:
            raise ConnectorError(f"freshteam: {url} does not state its job count")
        if int(count.group("n")) != len(jobs):
            raise ConnectorError(
                f"freshteam: {board_identifier} states {count.group('n')} jobs, "
                f"links {len(jobs)}; refusing a partial list"
            )
        empty = [j.external_id for j in jobs.values() if not j.title]
        if empty:
            raise ConnectorError(f"freshteam: no title for jobs {empty[:5]} (template changed?)")
        log.info("freshteam %s: %d jobs", board_identifier, len(jobs))
        return list(jobs.values())

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = job.source_url or ""
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if _JOB_PATH.match(urlsplit(str(response.url)).path) is None:
            return None  # redirected away from the job (e.g. to /jobs): it is gone
        tree = LexborHTMLParser(response.text)
        content = tree.css_first(".job-details-content")
        if content is None or tree.css_first(".job-details-header h1") is None:
            raise ConnectorError(f"freshteam: {url} has no job details (template changed?)")
        for node in content.css("div, p"):
            if _text(node).startswith("Liquid error:"):
                node.decompose()
        posted_by = _POSTED_BY.search(_meta(tree, "og:description"))
        description = (content.inner_html or "").strip()
        return RawJob(
            **{
                **job.as_dict(),
                "description_html": description or None,
                "company": posted_by.group("name") if posted_by else job.company,
            }
        )
