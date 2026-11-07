"""SmartRecruiters Posting API.

Docs: https://developers.smartrecruiters.com/docs/posting-api
List:   GET https://api.smartrecruiters.com/v1/companies/{company}/postings?offset={n}&limit=100
        (paginated; **no descriptions**)
Detail: GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}
        (description sections + apply URL) -> this connector sets ``needs_detail``.

NOTE: as of 2026-11 ``api.smartrecruiters.com/robots.txt`` disallows ``/`` for every
user agent except LinkedInBot, so the shared HTTP client refuses these requests and
crawls are recorded as failures until access is arranged (see README).

URL formats recognised (``{c}`` = company identifier, case-sensitive):
    jobs.smartrecruiters.com/{c}[/{posting-id}-{slug}]
    careers.smartrecruiters.com/{c}
    jobs.smartrecruiters.com/oneclick-ui/company/{c}/...
    api.smartrecruiters.com/v1/companies/{c}/...
Custom-domain pages: any of the above in the page, or the job widget config
``company_code: "{c}"``.
"""

import re
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.models.enums import RemoteType
from jobindex.normalize import normalize_employment_type, parse_datetime, split_url

API = "https://api.smartrecruiters.com/v1/companies"
PAGE_SIZE = 100
MAX_PAGES = 500
_RESERVED = {"oneclick-ui", "sr-jobs", "api", "static", "robots.txt", "favicon.ico"}
_COMPANY = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


@register
class SmartRecruitersConnector(BaseConnector):
    platform = "smartrecruiters"
    html_fingerprints = (re.compile(r"""company_code["']?\s*:\s*["'](?P<id>[A-Za-z0-9_-]+)["']"""),)
    needs_detail = True
    detail_fields = ("description_html", "description_text", "apply_url")

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, _ = split_url(url)
        company: str | None = None
        if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"} and segments:
            if segments[0] == "oneclick-ui" and segments[1:2] == ["company"] and len(segments) > 2:
                company = segments[2]
            elif segments[0] not in _RESERVED:
                company = segments[0]
        elif host == "api.smartrecruiters.com" and segments[:2] == ["v1", "companies"]:
            company = segments[2] if len(segments) >= 3 else None
        if company and _COMPANY.match(company):
            return company
        return None

    @classmethod
    def detect_in_html(cls, html: str) -> dict[str, int]:
        if "smartrecruiters" not in html.lower():
            return {}  # "company_code" alone is too generic to trust
        return super().detect_in_html(html)

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://jobs.smartrecruiters.com/{board_identifier}"

    async def fetch_company_name(self, board_identifier: str) -> str | None:
        data = await self.http.get_json(f"{API}/{board_identifier}/postings", params={"limit": 1})
        content = data.get("content") if isinstance(data, dict) else None
        if content:
            name = (content[0].get("company") or {}).get("name")
            return str(name) if name else None
        return None

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        postings: dict[str, dict[str, Any]] = {}
        total: int | None = None
        for page in range(MAX_PAGES):
            data = await self.http.get_json(
                f"{API}/{board_identifier}/postings",
                params={"offset": page * PAGE_SIZE, "limit": PAGE_SIZE},
            )
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise ConnectorError("smartrecruiters: response has no 'content' list")
            total = data.get("totalFound")
            for posting in data["content"]:
                postings[str(posting["id"])] = posting
            if not data["content"] or not isinstance(total, int) or (page + 1) * PAGE_SIZE >= total:
                break
        else:
            raise ConnectorError(f"smartrecruiters: more than {MAX_PAGES} pages")
        if isinstance(total, int) and len(postings) < total:
            # Postings shifted between pages; a partial list would close live jobs.
            raise ConnectorError(f"smartrecruiters: got {len(postings)} of {total} postings")
        return [self._map(board_identifier, p) for p in postings.values()]

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        try:
            data = await self.http.get_json(f"{API}/{board_identifier}/postings/{job.external_id}")
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None  # unpublished between list and detail request
            raise
        if not isinstance(data, dict):
            raise ConnectorError("smartrecruiters: detail response is not an object")
        sections = ((data.get("jobAd") or {}).get("sections")) or {}
        html_parts = []
        for key in _SECTIONS:
            section = sections.get(key) or {}
            if section.get("text"):
                title = section.get("title")
                html_parts.append((f"<h3>{title}</h3>" if title else "") + section["text"])
        return RawJob(
            **{
                **job.as_dict(),
                "description_html": "\n".join(html_parts) or None,
                "description_text": None,
                "apply_url": data.get("applyUrl") or job.apply_url,
            }
        )

    @staticmethod
    def _map(company: str, p: dict[str, Any]) -> RawJob:
        location = p.get("location") or {}
        if location.get("remote"):
            remote_type = RemoteType.REMOTE
        elif location.get("hybrid"):
            remote_type = RemoteType.HYBRID
        elif "hybrid" in location and "remote" in location:
            remote_type = RemoteType.ONSITE  # both flags explicitly false
        else:
            remote_type = RemoteType.UNKNOWN
        employment = (p.get("typeOfEmployment") or {}).get("label")
        return RawJob(
            external_id=str(p["id"]),
            title=str(p.get("name") or "").strip(),
            company=(p.get("company") or {}).get("name"),
            location_raw=location.get("fullLocation") or None,
            city=location.get("city") or None,
            region=location.get("region") or None,
            country=(location.get("country") or "").upper() or None,
            remote_type=remote_type,
            employment_type=normalize_employment_type(employment),
            posted_at=parse_datetime(p.get("releasedDate")),
            source_url=f"https://jobs.smartrecruiters.com/{company}/{p['id']}",
        )
