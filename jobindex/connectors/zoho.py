"""Zoho Recruit career sites (``{tenant}.zohorecruit.{com|eu|in|com.au|jp}/jobs/{portal}``).

List: GET {host}/jobs/{portal}: every published job of the portal is embedded in the page as
      ``<input type="hidden" id="jobs" value="[...]">`` (JSON: id, Posting_Title, City,
      State, Country, Remote_Job, Job_Type, Publish; some tenants include
      Job_Description). Jobs with ``Publish: false`` are skipped. A page without that
      input is an error (template changed), an empty list is a board without jobs.
Job:  GET {host}/jobs/{portal}/{id}: no JSON-LD; the page script holds the posting as
      ``var jobs = JSON.parse('[{...}]')`` (Posting_Title, Job_Description HTML,
      City/State/Country/Zip_Code, Remote_Job, Job_Type, Date_Opened, Salary). A job that
      is gone answers with a short "This job is no longer available." page (stored
      closed). The employer is ``og:site_name``.

robots.txt (same on every tenant) is ``Disallow: /`` with ``Allow: /jobs/*`` (and a few
/recruit/*.na pages): only the career pages are read. ``/recruit/downloadrssfeed`` and
``/recruit/sitemapfeed`` are allowed but return the portal HTML without the tenant's feed
parameters, so they are not used. Pages are ~1.7 MB each (the app is inlined).

Board identifier: ``{host}/{portal}`` (``us-careers.zohorecruit.in/Careers``; the portal
name is case-sensitive). Generic machinery (``generic_jsonld``): pages are fetched for new
jobs and every GENERIC_REFRESH_DAYS, with checkpoints.
Verified on cause-capacity.zohorecruit.com/Careers and us-careers.zohorecruit.in/Careers
(2026-11-08); eu.zohorecruit.eu/careers (Zoho Europe) lists 0 jobs.
"""

import json
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import urlsplit

from jobindex.connectors.base import ConnectorError, RawJob, company_name_from_page
from jobindex.connectors.generic_jsonld import GenericJsonLdConnector
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type

_HOST = re.compile(r"^[a-z0-9][a-z0-9-]*\.zohorecruit\.(?:com|eu|in|com\.au|jp|com\.cn|ca|sa)$")
_NOT_TENANTS = {"www", "recruit", "help", "static", "api"}
_PORTAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_JOBS_INPUT = re.compile(r"<input\b[^>]*\bid=[\"']jobs[\"'][^>]*>", re.I)
_VALUE = re.compile(r"\bvalue=(?:\"([^\"]*)\"|'([^']*)')", re.I)
_JOBS_VAR = re.compile(r"var jobs = JSON\.parse\('((?:[^'\\]|\\.)*)'\);")
_JS_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.S)


@register
class ZohoRecruitConnector(GenericJsonLdConnector):
    platform = "zoho"
    company_name_sources = ("og:site_name",)

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        host = (parts.hostname or "").lower()
        segments = [s for s in parts.path.split("/") if s]
        if not _HOST.match(host) or host.split(".", 1)[0] in _NOT_TENANTS:
            return None
        if len(segments) < 2 or segments[0] != "jobs" or not _PORTAL.match(segments[1]):
            return None
        return f"{host}/{segments[1]}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        host, _, portal = board_identifier.partition("/")
        return f"https://{host}/jobs/{portal}"

    def job_id(self, url: str, board_identifier: str) -> str | None:
        parts = urlsplit(url)
        host, _, portal = board_identifier.partition("/")
        segments = [s for s in parts.path.split("/") if s]
        if (parts.hostname or "").lower() != host or len(segments) < 3:
            return None
        if segments[:2] != ["jobs", portal] or not segments[2].isdigit():
            return None
        return segments[2]

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        url = self.board_url(board_identifier)
        html = await self._get_text(url)
        jobs = _listed_jobs(html, url)
        return [
            (f"{url}/{job['id']}", None)
            for job in jobs
            if job.get("Publish", True) and str(job.get("id", "")).isdigit()
        ]

    def parse_page_html(self, html: str, stub: RawJob) -> RawJob | None:
        match = _JOBS_VAR.search(html)
        if match is None:
            return None
        try:
            items = json.loads(_js_unescape(match.group(1)))
        except ValueError as exc:
            raise ConnectorError(f"zoho: unreadable job data on {stub.source_url}") from exc
        item = next((i for i in items if str(i.get("id")) == stub.external_id), None)
        if not isinstance(item, dict):
            return None
        title = str(item.get("Posting_Title") or item.get("Job_Opening_Name") or "").strip()
        city, region, country = (_text(item, k) for k in ("City", "State", "Country"))
        location = ", ".join(v for v in (city, region, country) if v) or None
        remote = item.get("Remote_Job")
        return RawJob(
            external_id=stub.external_id,
            title=title,
            company=company_name_from_page(html, self.company_name_sources),
            description_html=_text(item, "Job_Description"),
            location_raw=location,
            city=city,
            region=region,
            country=country,
            remote_type=RemoteType.REMOTE if remote is True else RemoteType.UNKNOWN,
            employment_type=normalize_employment_type(_text(item, "Job_Type")),
            posted_at=_date(_text(item, "Date_Opened")),
            source_url=stub.source_url,
            apply_url=stub.source_url,  # the Apply button is on the job page
        )


def _listed_jobs(html: str, url: str) -> list[dict[str, Any]]:
    tag = _JOBS_INPUT.search(html)
    value = _VALUE.search(tag.group(0)) if tag else None
    if value is None:
        raise ConnectorError(f"zoho: no job list on {url}; template changed?")
    try:
        jobs = json.loads(unescape(value.group(1) or value.group(2) or "[]"))
    except ValueError as exc:
        raise ConnectorError(f"zoho: unreadable job list on {url}") from exc
    if not isinstance(jobs, list):
        raise ConnectorError(f"zoho: unexpected job list on {url}")
    return [job for job in jobs if isinstance(job, dict)]


def _js_unescape(value: str) -> str:
    """Body of a single-quoted JS string literal: ``\\x22`` -> ``"``, ``\\-`` -> ``-``,
    ``\\\\`` -> ``\\`` (what remains is the JSON text)."""

    def one(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] in "xu" and len(code) > 1:
            return chr(int(code[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(code, code)

    return _JS_ESCAPE.sub(one, value)


def _text(item: dict[str, Any], key: str) -> str | None:
    value = item.get(key)
    return str(value).strip() or None if value is not None else None


def _date(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(value or "", "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
