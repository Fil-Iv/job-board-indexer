"""SmartRecruiters.

Two code paths; the HTML one is the default.

**Career pages (default)** — ``careers.smartrecruiters.com`` / ``jobs.smartrecruiters.com``
are allowed by robots.txt (2026-11-07: careers' robots.txt redirects to an HTML page with
no rules, jobs' returns 404, i.e. allow all).
    List:   GET careers.smartrecruiters.com/{c}                       (page 0 + group counts)
            GET careers.smartrecruiters.com/{c}/api/groups?page=N     (more location groups)
            GET careers.smartrecruiters.com/{c}/api/more?page=N       (flat list of ALL jobs,
                                                                      ~24 per page)
            The sum of the groups' ``data-qty`` is the board's total; the flat list must
            reach it (verified: Ubisoft2 298 = 298), else the fetch is refused.
    Detail: GET jobs.smartrecruiters.com/{c}/{id}  -> schema.org JobPosting microdata.

    Company opt-outs: ``www.smartrecruiters.com/robots.txt`` disallows ``/{Company}/*`` for
    ~70 companies (e.g. ``/BoschGroup/*``). Those legacy pages redirect to the career
    hosts, so we treat such a rule as the company opting out of crawlers and refuse to
    crawl it (RobotsDisallowedError) even though the career hosts would allow it.

**Posting API (disabled)** — ``api.smartrecruiters.com/robots.txt`` disallows ``/`` for
every agent but LinkedInBot. Kept for when written permission exists:
``SMARTRECRUITERS_USE_API=true``. It still goes through the robots-respecting client.
    List:   GET api.smartrecruiters.com/v1/companies/{c}/postings?offset=&limit=100
    Detail: GET api.smartrecruiters.com/v1/companies/{c}/postings/{id}

URL formats recognised (``{c}`` = company identifier, case-sensitive):
    jobs.smartrecruiters.com/{c}[/{posting-id}-{slug}]
    careers.smartrecruiters.com/{c}
    jobs.smartrecruiters.com/oneclick-ui/company/{c}/...
    api.smartrecruiters.com/v1/companies/{c}/...
Custom-domain pages: any of the above in the page, or the job widget config
``company_code: "{c}"``.
"""

import logging
import re
from dataclasses import replace
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError, HttpClient, RobotsDisallowedError
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    html_to_text,
    normalize_employment_type,
    parse_datetime,
    remote_type_from_value,
    split_url,
)
from jobindex.schemaorg import extract_job_postings, to_raw_job

log = logging.getLogger(__name__)

API = "https://api.smartrecruiters.com/v1/companies"
CAREERS = "https://careers.smartrecruiters.com"
JOBS = "https://jobs.smartrecruiters.com"
OPT_OUT_PROBE = "https://www.smartrecruiters.com/{company}/"
MAX_HTML_PAGES = 1000
_JOB_ID = re.compile(r"/(\d{6,})(?:-|$|\?)")
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
    # The HTML list only has title + employment type; everything else is in the detail page.
    detail_cache_key = ("title",)
    detail_fields = (
        "description_html",
        "description_text",
        "company",
        "location_raw",
        "city",
        "region",
        "country",
        "remote_type",
        "posted_at",
        "apply_url",
    )

    def __init__(self, http: HttpClient, *, use_api: bool | None = None) -> None:
        super().__init__(http)
        self.use_api = get_settings().smartrecruiters_use_api if use_api is None else use_api

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
        if self.use_api:
            return await self._fetch_company_name_api(board_identifier)
        await self.check_company_opt_out(board_identifier)
        tree = LexborHTMLParser((await self.http.get(f"{CAREERS}/{board_identifier}")).text)
        node = tree.css_first('meta[property="og:title"]') or tree.css_first("title")
        title: str | None = None
        if node is not None:
            title = node.attributes.get("content") if node.tag == "meta" else node.text()
        if title and title.strip().lower().startswith("careers at "):
            return title.strip()[len("careers at ") :].strip() or None
        return None

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        if self.use_api:
            return await self._fetch_jobs_api(board_identifier)
        return await self._fetch_jobs_html(board_identifier)

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        if self.use_api:
            return await self._fetch_job_detail_api(board_identifier, job)
        return await self._fetch_job_detail_html(board_identifier, job)

    # -- career pages (default) --------------------------------------------

    async def check_company_opt_out(self, company: str) -> None:
        """Raise if www.smartrecruiters.com/robots.txt opts this company out."""
        probe = OPT_OUT_PROBE.format(company=company)
        allowed, rule = await self.http.robots_verdict(probe)
        if not allowed:
            raise RobotsDisallowedError(
                f"company opted out: www.smartrecruiters.com/robots.txt has '{rule}'",
                url=probe,
            )

    async def _fetch_jobs_html(self, company: str) -> list[RawJob]:
        await self.check_company_opt_out(company)
        expected = await self._expected_total(company)
        jobs = await self._flat_listing(company)
        if len(jobs) < expected:
            log.info(
                "smartrecruiters %s: got %d of %d, re-listing once", company, len(jobs), expected
            )
            jobs = await self._flat_listing(company)
            if len(jobs) < expected:
                raise ConnectorError(
                    f"smartrecruiters: got {len(jobs)} of {expected} postings; "
                    "refusing a partial result"
                )
        return list(jobs.values())

    async def _expected_total(self, company: str) -> int:
        """Sum of ``data-qty`` over every location group (page 0 + /api/groups pages)."""
        first = LexborHTMLParser((await self.http.get(f"{CAREERS}/{company}")).text)
        loader = first.css_first("[data-groups-pages]")
        if loader is None:
            if first.css_first("section.js-group") is None:
                return 0  # board with no openings
            raise ConnectorError("smartrecruiters: career page has no group pager")
        pages = int(loader.attributes.get("data-groups-pages") or 1)
        total = _qty(first)
        for page in range(1, pages):
            response = await self.http.get(f"{CAREERS}/{company}/api/groups", params={"page": page})
            total += _qty(LexborHTMLParser(response.text))
        return total

    async def _flat_listing(self, company: str) -> dict[str, RawJob]:
        jobs: dict[str, RawJob] = {}
        for page in range(MAX_HTML_PAGES):
            response = await self.http.get(f"{CAREERS}/{company}/api/more", params={"page": page})
            batch = _listing_jobs(company, LexborHTMLParser(response.text))
            new = {k: v for k, v in batch.items() if k not in jobs}
            if not new:
                return jobs
            jobs.update(new)
        raise ConnectorError(f"smartrecruiters: more than {MAX_HTML_PAGES} listing pages")

    async def _fetch_job_detail_html(self, company: str, job: RawJob) -> RawJob | None:
        url = f"{JOBS}/{company}/{job.external_id}"
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if f"/{job.external_id}" not in str(response.url):
            return None  # redirected to the board: posting was unpublished
        items = extract_job_postings(response.text)
        if not items:
            raise ConnectorError(f"smartrecruiters: no JobPosting markup on {url}")
        detail = to_raw_job(items[0], external_id=job.external_id, source_url=job.source_url)
        tree = LexborHTMLParser(response.text)
        node = tree.css_first("spl-job-location")
        workplace = node.attributes.get("workplacetype") if node is not None else None
        remote_type = remote_type_from_value((workplace or "").replace("_", ""))
        return replace(
            job,
            description_html=detail.description_html,
            description_text=None,
            company=detail.company or job.company,
            location_raw=detail.location_raw,
            city=detail.city,
            region=detail.region,
            country=detail.country,
            remote_type=remote_type if remote_type is not RemoteType.UNKNOWN else job.remote_type,
            employment_type=job.employment_type or detail.employment_type,
            posted_at=detail.posted_at,
            apply_url=url,
        )

    # -- Posting API (disabled unless SMARTRECRUITERS_USE_API) -------------

    async def _fetch_company_name_api(self, board_identifier: str) -> str | None:
        data = await self.http.get_json(f"{API}/{board_identifier}/postings", params={"limit": 1})
        content = data.get("content") if isinstance(data, dict) else None
        if content:
            name = (content[0].get("company") or {}).get("name")
            return str(name) if name else None
        return None

    async def _fetch_jobs_api(self, board_identifier: str) -> list[RawJob]:
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

    async def _fetch_job_detail_api(self, board_identifier: str, job: RawJob) -> RawJob | None:
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


def _qty(tree: LexborHTMLParser) -> int:
    return sum(int(sec.attributes.get("data-qty") or 0) for sec in tree.css("section.js-group"))


def _listing_jobs(company: str, tree: LexborHTMLParser) -> dict[str, RawJob]:
    jobs: dict[str, RawJob] = {}
    for link in tree.css("a.js-job-ad-link"):
        href = link.attributes.get("href") or ""
        match = _JOB_ID.search(href)
        if not match:
            continue
        job_id = match.group(1)
        title_node = link.css_first(".job-title")
        desc = link.css_first(".job-desc")
        title = html_to_text(title_node.html) if title_node is not None and title_node.html else ""
        jobs[job_id] = RawJob(
            external_id=job_id,
            title=title or "",
            employment_type=normalize_employment_type(desc.text(strip=True) if desc else None),
            source_url=f"{JOBS}/{company}/{job_id}",
        )
    return jobs
