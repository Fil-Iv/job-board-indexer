"""IBM Kenexa BrassRing career sites (``sjobs.brassring.com``) — sites with up to 50 jobs.

List: GET {host}/TGnewUI/Search/Home/HomeWithPreLoad?partnerid={p}&siteid={s}
      &PageType=searchResults — the server renders the first search page into
      ``<input id="preLoadJSON" value="{...}">``: ``searchResultsResponse.Jobs.Job`` (at most
      50 jobs: ``reqid``, ``jobtitle``, ``lastupdated``, ...) and ``JobsCount``, the total.
      This GET ignores every paging/keyword/sort parameter; further pages exist only as
      POST /TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs with the session value and RFT
      token the page issues, which are not used. So a site is crawled only when its
      ``JobsCount`` is 50 or less and equals the jobs received; a bigger site fails with
      ``ConnectorError`` (never a partial list).
Job:  GET ...&PageType=JobDetails&jobid={reqid} — ``preLoadJSON.Jobdetails``: ``Title``,
      ``isActive`` and ``JobDetailQuestions`` (labelled fields such as "Job Posting",
      "Qualifications", "Salary Range"; the location is the field named in
      ``JobDetailFieldsToDisplay.Position3``). An inactive or missing job is closed.

robots.txt is 404 on sjobs.brassring.com (everything allowed). The employer is the page
``<title>`` ("Search Jobs at | Halifax Regional Municipality" on the home page, "Job Search |
..." on the preload pages).

Board identifier: ``{host}/{partnerid}/{siteid}`` (``sjobs.brassring.com/25749/5764``).
Verified on Halifax Regional Municipality (25749/5764, 36 jobs) and Fairfax County Public
Schools (25103/5041, 17 jobs), 2026-11-08. Of 19 other live sites checked, 14 have more
than 50 jobs (54 to 24,005) and 5 have none.
"""

import json
import re
from datetime import UTC, datetime
from html import escape, unescape
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register

MAX_JOBS = 50  # what the server-rendered search page holds
_HOST = re.compile(r"^(?:[a-z0-9-]+\.)?brassring\.com$")
_PRELOAD = re.compile(r"id=\"preLoadJSON\"[^>]*?\svalue=\"([^\"]*)\"")
_TITLE = re.compile(r"^(?:Search Jobs at|Job Search)\s*\|\s*(?P<name>.+)$", re.I)
#: Detail fields that are bookkeeping, not posting content.
_SKIP_ZONES = {
    "reqid",
    "hotjob",
    "siteid",
    "clientid",
    "gqid",
    "jobreqlanguage",
    "latitude",
    "longitude",
    "lastupdated",
    "autoreq",
    "jobtitle",
}


@register
class BrassRingConnector(BaseConnector):
    platform = "brassring"
    needs_detail = True
    company_name_sources = (_TITLE,)
    detail_cache_key: ClassVar[tuple[str, ...]] = ("title", "posted_at")

    @classmethod
    def detect(cls, url: str) -> str | None:
        parts = urlsplit(url if "://" in url else "https://" + url)
        host = (parts.hostname or "").lower()
        if not _HOST.match(host) or "/tgnewui/" not in parts.path.lower():
            return None
        params = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
        partner, site = params.get("partnerid", ""), params.get("siteid", "")
        if not (partner.isdigit() and site.isdigit()):
            return None
        return f"{host}/{partner}/{site}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        host, partner, site = cls._split(board_identifier)
        return f"https://{host}/TGnewUI/Search/Home/Home?partnerid={partner}&siteid={site}"

    @staticmethod
    def _split(board_identifier: str) -> tuple[str, str, str]:
        parts = board_identifier.split("/")
        if len(parts) != 3:
            raise ConnectorError(f"brassring: bad board identifier {board_identifier!r}")
        return parts[0], parts[1], parts[2]

    def _preload_url(self, board_identifier: str, **params: str) -> str:
        host, partner, site = self._split(board_identifier)
        query = urlencode({"partnerid": partner, "siteid": site, **params})
        return f"https://{host}/TGnewUI/Search/Home/HomeWithPreLoad?{query}"

    async def _preload(self, url: str) -> dict[str, Any]:
        response = await self.http.get(url)
        match = _PRELOAD.search(response.text)
        if match is None:
            raise ConnectorError(f"brassring: no preLoadJSON on {url}; page changed?")
        try:
            data = json.loads(unescape(match.group(1)))
        except ValueError as exc:
            raise ConnectorError(f"brassring: unreadable preLoadJSON on {url}") from exc
        if not isinstance(data, dict):
            raise ConnectorError(f"brassring: unexpected preLoadJSON on {url}")
        return data

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = self._preload_url(board_identifier, PageType="searchResults")
        data = await self._preload(url)
        results = data.get("searchResultsResponse")
        if not isinstance(results, dict) or not isinstance(results.get("JobsCount"), int):
            raise ConnectorError(f"brassring: no search results on {url}")
        total = results["JobsCount"]
        jobs = (results.get("Jobs") or {}).get("Job") or []
        if total > MAX_JOBS:
            raise ConnectorError(
                f"brassring: {board_identifier} has {total} jobs; only the first {MAX_JOBS} "
                "are available without page-issued tokens, so the site cannot be listed "
                "completely"
            )
        found: dict[str, RawJob] = {}
        for job in jobs:
            fields = {q.get("QuestionName"): q.get("Value") for q in job.get("Questions") or []}
            req_id = str(fields.get("reqid") or "")
            if not req_id.isdigit():
                raise ConnectorError(f"brassring: job without reqid on {url}")
            found[req_id] = RawJob(
                external_id=req_id,
                title=str(fields.get("jobtitle") or "").strip(),
                posted_at=_date(fields.get("lastupdated")),
                source_url=self._preload_url(board_identifier, PageType="JobDetails", jobid=req_id),
            )
        if len(found) != total:
            raise ConnectorError(
                f"brassring: {len(found)} distinct jobs received but JobsCount is {total}"
            )
        return list(found.values())

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        data = await self._preload(job.source_url or "")
        details = data.get("Jobdetails")
        if not isinstance(details, dict) or not details.get("isActive"):
            return None
        questions = details.get("JobDetailQuestions") or []
        display = data.get("JobDetailFieldsToDisplay") or {}
        location_zones = display.get("Position3") or ["location"]
        location = next(
            (
                str(q.get("AnswerValue")).strip()
                for q in questions
                if q.get("VerityZone") in location_zones and q.get("AnswerValue")
            ),
            None,
        )
        sections = [
            f"<h3>{escape(_label(q))}</h3><div>{q.get('AnswerValue')}</div>"
            for q in questions
            if q.get("QuestionName")
            and q.get("AnswerValue")
            and q.get("VerityZone") not in _SKIP_ZONES
            and q.get("VerityZone") not in location_zones
        ]
        return RawJob(
            **{
                **job.as_dict(),
                "title": str(details.get("Title") or job.title).strip(),
                "description_html": "".join(sections) or None,
                "location_raw": location,
                "apply_url": job.source_url,
            }
        )


def _label(question: dict[str, Any]) -> str:
    """``"Salary Grade<br><a ...>[Salary Information]</a>"`` -> ``"Salary Grade"``."""
    text = re.sub(r"<br\s*/?>", " ", str(question.get("QuestionName") or ""), flags=re.I)
    text = re.sub(r"<a\b.*?</a>|<[^>]+>", "", text, flags=re.I | re.S)
    return " ".join(unescape(text).split())


def _date(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value or ""), "%d-%b-%Y").replace(tzinfo=UTC)
    except ValueError:
        return None
