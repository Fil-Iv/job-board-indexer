"""JazzHR career pages (``{company}.applytojob.com/apply``): HTML list + HTML job pages.

List:   GET https://{company}.applytojob.com/apply
        Server-rendered, all open jobs on one page: ``div.jobs-list li.list-group-item``,
        each with ``h3.list-group-item-heading a`` -> ``/apply/{id}/{slug}`` and
        ``ul.list-group-item-text li`` (location, then department). No pagination and no
        job count to check against, so the list page must have the ``jobs-list``
        container (else: error) and the generic job-drop guard (``max_job_drop``,
        GENERIC_MAX_JOB_DROP) refuses a result that suddenly lost most jobs.
        Unknown companies redirect to www.jazzhr.com (no container): error.
Detail: GET https://{company}.applytojob.com/apply/{id}/{slug}  (only for new/changed)
        ``.job-header h2`` (title), ``.job-attributes-container [title=Location|Type]``,
        ``#job-description`` (HTML). No JSON-LD and no posting date. Closed jobs answer
        410 (unknown ids too).

Not used: the XML export ``app.jazz.co/feeds/export/jobs/{company}`` (410 Gone); the
official API (needs the customer's key); and the sitemaps robots.txt names
(``app.jazz.co/feeds/google/xml/0..4``, ~22,000 URLs / 4 MB each, all tenants mixed).
robots.txt disallows only ``/cb`` (and SemrushBot/dotbot entirely).

Board identifier: the subdomain, e.g. ``firstadvantage``.

URL formats recognised:
    {company}.applytojob.com[/apply[/{id}/{slug}]]
Custom-domain career pages: links/iframes to the above (generic HTML detection).

Verified on firstadvantage, ilsos and landing (2026-11-07).
"""

import re
from dataclasses import replace
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.normalize import normalize_employment_type, split_url

_SUFFIX = ".applytojob.com"
_COMPANY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_RESERVED = {"www", "app", "api", "landing-page"}
_JOB_PATH = re.compile(r"^/apply/(?P<id>[A-Za-z0-9]{10})(?:/[^/?#]*)?/?$")


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


@register
class JazzHRConnector(BaseConnector):
    platform = "jazzhr"
    needs_detail = True
    detail_fields = ("description_html", "description_text", "employment_type")

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
        return f"https://{board_identifier}{_SUFFIX}/apply"

    def max_job_drop(self) -> float | None:
        return get_settings().generic_max_job_drop

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = self.board_url(board_identifier)
        response = await self.http.get(url)
        if split_url(str(response.url))[0] != f"{board_identifier}{_SUFFIX}":
            raise ConnectorError(f"jazzhr: {url} redirected to {response.url} (unknown company?)")
        tree = LexborHTMLParser(response.text)
        container = tree.css_first(".jobs-list")
        if container is None:
            raise ConnectorError(f"jazzhr: no job list on {url}; template changed?")
        host = f"{board_identifier}{_SUFFIX}"
        jobs: dict[str, RawJob] = {}
        for item in container.css("li.list-group-item"):
            link = item.css_first(".list-group-item-heading a")
            href = urlsplit((link.attributes.get("href") or "") if link else "")
            match = _JOB_PATH.match(href.path)
            if link is None or match is None or (href.hostname or host).lower() != host:
                continue
            details = [_clean(li.text()) for li in item.css("ul.list-group-item-text li")]
            job_url = f"https://{host}{href.path.rstrip('/')}"
            jobs[match["id"]] = RawJob(
                external_id=match["id"],
                title=_clean(link.text()),
                location_raw=(details[0] or None) if details else None,
                source_url=job_url,
                apply_url=job_url,
            )
        return list(jobs.values())

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = job.source_url or ""
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if _JOB_PATH.match(response.url.path) is None:
            return None  # redirected away: the job is closed
        tree = LexborHTMLParser(response.text)
        description = tree.css_first("#job-description")
        if description is None:
            raise ConnectorError(f"jazzhr: no #job-description on {url}; template changed?")
        attributes = {
            n.attributes.get("title"): _clean(n.text())
            for n in tree.css(".job-attributes-container [title]")
        }
        return replace(
            job,
            description_html=(description.inner_html or "").strip() or None,
            employment_type=normalize_employment_type(attributes.get("Type")),
        )
