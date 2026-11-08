"""HireBridge career centers (``recruit.hirebridge.com/v3/Jobs/list.aspx?cid={cid}``): HTML.

List:   GET https://recruit.hirebridge.com/v3/Jobs/list.aspx?cid={cid}
        ASP.NET page, every open job on one page (Menards: 870, 2026-11-07):
        ``<li><span class="job"><a href="/v3/Jobs/JobDetails.aspx?cid=..&jid=..&locvalue=..">
        title</a></span><span class="department">location</span></li>``. A job posted at
        several locations has one link per location (EAB: 99 links, 44 jobs); they become
        one posting with the locations joined. No job count, so the generic job-drop
        guard (``max_job_drop``) protects against a broken page. Unknown cids redirect to
        ``/v3/Application/AppErrMsg.aspx``: error.
Detail: GET .../JobDetails.aspx?cid={cid}&jid={jid}&locvalue={first location}
        (only for new/changed jobs). Elements by id suffix: ``_jobtitle``, ``_jobtype``,
        ``_jobloc`` (used when the list has no location: Menard, 2026-11-08),
        ``_jobdesc``, ``_skilldesc``, ``_qualificationsdesc`` (HTML). No JSON-LD. A closed
        or unknown job is a 200 page with an empty ``_jobtitle``: closed.
robots.txt disallows /application, /company and /jobseeker2|4 only.

Board identifier: the numeric company id (``cid``), e.g. ``7856``.

URL formats recognised:
    recruit.hirebridge.com/v3/Jobs/list.aspx?cid={cid}
    recruit.hirebridge.com/v3/Jobs/JobDetails.aspx?cid={cid}&jid={jid}
Custom-domain career pages: links/iframes to the above (generic HTML detection).

Verified on EAB (7856) and Menard, Inc (5535), 2026-11-07.
"""

import hashlib
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.normalize import normalize_employment_type, split_url

HOST = "recruit.hirebridge.com"
_PAGES = {"list.aspx", "jobdetails.aspx"}
_SECTIONS = (
    ("_jobdesc", None),
    ("_skilldesc", "Skills"),
    ("_qualificationsdesc", "Qualifications"),
)


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _by_suffix(tree: LexborHTMLParser, suffix: str) -> LexborNode | None:
    return tree.css_first(f'[id$="{suffix}"]')


@register
class HireBridgeConnector(BaseConnector):
    platform = "hirebridge"
    needs_detail = True
    detail_fields = ("description_html", "description_text", "employment_type", "location_raw")
    # The list's own location is in ``list_version``: the stored ``location_raw`` may come
    # from the job page (when the list has none), so it cannot be compared with the list.
    detail_cache_key = ("title", "list_version")

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, query = split_url(url)
        lowered = [s.lower() for s in segments]
        if host != HOST or lowered[:2] != ["v3", "jobs"]:
            return None
        if len(lowered) > 2 and lowered[2] not in _PAGES:
            return None
        cid = {k.lower(): v for k, v in query.items()}.get("cid", [""])[0]
        return cid if cid.isdigit() else None

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{HOST}/v3/Jobs/list.aspx?cid={board_identifier}"

    def max_job_drop(self) -> float | None:
        return get_settings().generic_max_job_drop

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        url = self.board_url(board_identifier)
        response = await self.http.get(url)
        if response.url.path.lower() != "/v3/jobs/list.aspx":
            raise ConnectorError(f"hirebridge: {url} redirected to {response.url} (unknown cid?)")
        tree = LexborHTMLParser(response.text)
        title = tree.css_first("title")
        company = _clean(title.text()).removesuffix("Career Center").strip() if title else ""
        rows: dict[str, tuple[str, str, list[str]]] = {}  # jid -> (title, first URL, locations)
        for link in tree.css("span.job a"):
            parts = urlsplit(link.attributes.get("href") or "")
            query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
            jid = query.get("jid", "")
            if parts.path.lower() != "/v3/jobs/jobdetails.aspx" or not jid.isdigit():
                continue
            if query.get("cid") != board_identifier:
                continue
            item = link.parent.parent if link.parent else None
            location = item.css_first("span.department") if item else None
            title_text = _clean(link.text())
            job_url = f"https://{HOST}{parts.path}?{parts.query}"
            known = rows.setdefault(jid, (title_text, job_url, []))
            place = _clean(location.text()) if location else ""
            if place and place not in known[2]:
                known[2].append(place)
        return [
            RawJob(
                external_id=jid,
                title=title_text,
                company=company or None,
                location_raw="; ".join(places) or None,
                source_url=job_url,
                apply_url=job_url,
                list_version="loc:" + hashlib.sha1("; ".join(places).encode()).hexdigest(),
            )
            for jid, (title_text, job_url, places) in rows.items()
        ]

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        url = job.source_url or ""
        try:
            response = await self.http.get(url)
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if response.url.path.lower() != "/v3/jobs/jobdetails.aspx":
            return None  # redirected to an error page
        tree = LexborHTMLParser(response.text)
        title = _by_suffix(tree, "_jobtitle")
        if title is None:
            raise ConnectorError(f"hirebridge: no job title element on {url}; template changed?")
        if not _clean(title.text()):
            return None  # closed or unknown job: the page renders with empty fields
        sections = []
        for suffix, heading in _SECTIONS:
            node = _by_suffix(tree, suffix)
            html = (node.inner_html or "").strip() if node else ""
            if _clean(node.text() if node else ""):
                sections.append(f"<h3>{heading}</h3>{html}" if heading else html)
        job_type = _by_suffix(tree, "_jobtype")
        place = _by_suffix(tree, "_jobloc")
        return replace(
            job,
            description_html="\n".join(sections) or None,
            employment_type=normalize_employment_type(job_type.text()) if job_type else None,
            # some boards leave the list's location column empty (Menard): use the page's
            location_raw=job.location_raw or (_clean(place.text()) if place else "") or None,
        )
