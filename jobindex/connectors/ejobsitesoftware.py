"""ejobsitesoftware.com job boards (Aynsoft's PHP job-board script; RSS + JSON-LD).

List: ``/rss/job.php``, an RSS feed of the board's open jobs (``<links>`` = job page URL,
      plus title, description, company, location). The feed is **capped at 200 items**
      (eyetasticjobs.com: exactly 200, while 49 jobs in its per-industry feeds were
      missing from it). A feed with ``FEED_CAP`` or more items may be truncated, so the
      crawl fails: only boards whose whole list fits the feed are supported.
Job:  ``/{id}/{Title-Slug}.html`` with a JSON-LD JobPosting (title, description,
      datePosted, validThrough, employmentType, hiringOrganization, jobLocation,
      industry). A job page that answers HTTP 5xx keeps the feed's own fields (title,
      company, location, description): the job is in the complete list, so it stays open,
      it just has no posting date. (jobqlick.com: 3 of 108 job pages answered 500 on
      every request, 2026-11-11.)

Completeness was checked on both example boards (2026-11-11). The per-industry RSS
feeds (``/rss/{n}.xml``, linked from ``industry_rss.php``) are not usable there: they
are static files left from the vendor's demo install (connectagri.com: 2008, empty or
404; jobqlick.com: lorem-ipsum jobs of "JOBBOARD DEMO"). Instead, the per-industry
counts the site shows on ``job_search_by_industry.php`` were recomputed from the
``industry`` list of every job page in the feed, and matched exactly: connectagri.com
15 = 15 (10 jobs), jobqlick.com 71 = 71 (108 jobs, 34 without an industry).

robots.txt on both boards allows the feed and the job pages (jobqlick.com: Cloudflare's
managed file, ``search=yes``). ``job_search.php`` is a POST form and is not used.

Boards run on their owners' domains and are recognised by the script's resume page
link (``jobseeker_resume1.php``, in every page header) and the page's canonical URL.
Board identifier: the site host.
"""

import re
from dataclasses import replace
from html import unescape
from typing import Any

from jobindex.connectors.base import ConnectorError, RawJob
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register
from jobindex.http import FetchError

#: rss/job.php never lists more than this many jobs; a full feed may be truncated.
FEED_CAP = 200

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_EMPTY_COMPANY = {"", "na", "n/a"}


def _tag(item: str, name: str) -> str | None:
    match = re.search(rf"<{name}>(.*?)</{name}>", item, re.S)
    if match is None:
        return None
    value = match.group(1).strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3].strip()
    return value or None


def parse_feed_items(xml: str) -> list[dict[str, str | None]]:
    """The items of ``rss/job.php`` as plain dicts (values XML-unescaped once)."""
    items = []
    for body in _ITEM.findall(xml):
        fields = ("title", "description", "companyname", "location", "state", "country", "links")
        values = {name: _tag(body, name) for name in fields}
        items.append({k: unescape(v) if v is not None else None for k, v in values.items()})
    return items


@register
class EjobsiteSoftwareConnector(HostedBoardConnector):
    platform = "ejobsitesoftware"
    # The script's "post your resume" page, linked from the header of every page.
    page_fingerprint = re.compile(r"/jobseeker_resume1\.php\b")
    job_path = re.compile(r"/(?P<id>\d+)/[^/]+\.html")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._feed_jobs: dict[str, RawJob] = {}  # external_id -> job built from the feed

    async def listed_pages(self, board_identifier: str) -> list[tuple[str, str | None]]:
        url = f"https://{board_identifier}/rss/job.php"
        response = await self.http.get(url)
        # Declared iso-8859-1, but the body is UTF-8 on every board seen.
        xml = response.content.decode("utf-8", errors="replace")
        if not re.search(r"<rss\b", xml[:2000], re.I):
            raise ConnectorError(f"{url} is not an RSS feed")
        items = parse_feed_items(xml)
        if len(items) >= FEED_CAP:
            raise ConnectorError(
                f"{url} lists {len(items)} jobs, the feed's cap ({FEED_CAP}): the list may "
                "be truncated, so this board is not supported"
            )
        listed: list[tuple[str, str | None]] = []
        for item in items:
            link = item["links"]
            external_id = self.job_id(link or "", board_identifier)
            if link is None or external_id is None or not item["title"]:
                raise ConnectorError(f"{url}: item without a job link/title: {item}")
            self._feed_jobs[external_id] = self._feed_job(external_id, item)
            listed.append((link, None))
        return listed

    async def _fetch_page(self, board_identifier: str, stub: RawJob) -> RawJob:
        try:
            return await super()._fetch_page(board_identifier, stub)
        except FetchError as exc:
            feed_job = self._feed_jobs.get(stub.external_id)
            if exc.status_code is None or exc.status_code < 500 or feed_job is None:
                raise
            # The page is broken server-side; the job is still in the complete list.
            return replace(feed_job, source_url=stub.source_url, list_version=stub.list_version)

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        if (job.company or "").strip().lower() in _EMPTY_COMPANY:
            feed_job = self._feed_jobs.get(job.external_id)
            return replace(job, company=feed_job.company if feed_job else None)
        return job

    @staticmethod
    def _feed_job(external_id: str, item: dict[str, str | None]) -> RawJob:
        company = item["companyname"]
        if (company or "").strip().lower() in _EMPTY_COMPANY:
            company = None
        parts = [p for p in (item["location"], item["state"], item["country"]) if p]
        return RawJob(
            external_id=external_id,
            title=item["title"] or "",
            company=company,
            description_html=item["description"],
            location_raw=", ".join(dict.fromkeys(parts)) or None,
            source_url=item["links"],
            apply_url=item["links"],
        )
