"""SAP SuccessFactors Recruiting Marketing (RMK) career sites (generic sitemap + JSON-LD).

List: GET {site}/sitemap.xml: every open job as /job/{slug}/{id}/ with ``<lastmod>``.
Job:  microdata JobPosting; dates are Java ``Date.toString()`` values
      ("Mon Oct 12 00:00:00 UTC 2026"); hand-typed addressCountry fragments ("No") are
      dropped. Some tenant templates mark up only the description (NTT DATA, 2026-11-08):
      the title then comes from ``og:title`` and the location from the job URL, whose
      slug is ``{city}-{title}[-{region}-{postal code}]`` (``/job/Budapest-Sales-Manager-
      SAP-Solution-BU-1117/...`` -> "Budapest, BU"); a description placed outside the
      JobPosting scope is collected from every ``itemprop="description"`` block.

Board identifier: the career-site host (``careers.yash.com``). RMK sites are custom
domains, recognised by the RMK CDN (rmkcdn.successfactors.com) + canonical URL. The classic
career{n}.successfactors.com/career?company= page is JS-only and not covered.
Verified on careers.yash.com, careers.airindia.com and careers.nttdata-solutions.com
(2026-11-07).
"""

import re
from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import unquote, urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobindex.connectors.base import RawJob
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register
from jobindex.normalize import html_to_text, parse_datetime

_OG_TITLE = re.compile(
    r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']", re.I
)
_SLUG_WORD = re.compile(r"[^0-9A-Za-z]+")


@register
class SuccessFactorsConnector(HostedBoardConnector):
    platform = "successfactors"
    company_name_sources = (
        re.compile(r"^Jobs at (?P<name>.+)$"),
        re.compile(r"^(?P<name>.+?) Career Opportunities\b"),
    )
    # 2026-11-08: v1 og:title / URL-slug fallbacks; v2 description blocks outside the
    # JobPosting scope (adjust_page)
    detail_version = 2
    page_fingerprint = re.compile(r"//rmkcdn\.successfactors\.com/")
    job_path = re.compile(r"/job/[^/]+/(?P<id>\d+(?:-[a-z]{2}_[A-Z]{2})?)/?")

    def parse_date(self, value: str) -> datetime | None:
        try:  # "Mon Oct 12 00:00:00 UTC 2026"
            return datetime.strptime(value, "%a %b %d %H:%M:%S UTC %Y").replace(tzinfo=UTC)
        except ValueError:
            return parse_datetime(value)

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        # Tenants fill addressCountry by hand: keep ISO codes ("IN") and names, drop
        # fragments such as "No" (Air India), which would read as Norway.
        country = job.country
        if country and not (re.fullmatch(r"[A-Z]{2,3}", country) or len(country) > 3):
            parts = [p.strip() for p in (job.location_raw or "").split(",")]
            location = ", ".join(parts[:-1]) if parts and parts[-1] == country else None
            return replace(job, country=None, location_raw=location or job.location_raw)
        return job

    def adjust_page(self, job: RawJob, html: str) -> RawJob:
        changes: dict[str, Any] = {}
        if not job.title:
            match = _OG_TITLE.search(html)
            if match:
                changes["title"] = unescape(match.group(1)).strip()
        if not html_to_text(job.description_html or ""):
            # Some templates put the text in itemprop="description" blocks *outside* the
            # JobPosting itemscope (NTT DATA: banner image, then the text).
            blocks = [n.html or "" for n in LexborHTMLParser(html).css('[itemprop="description"]')]
            joined = "\n".join(b for b in blocks if html_to_text(b))
            if joined:
                changes["description_html"] = joined
        title = changes.get("title") or job.title
        if not (job.location_raw or job.city or job.country) and title and job.source_url:
            location = location_from_slug(job.source_url, title)
            if location:
                changes["location_raw"] = location
        return replace(job, **changes) if changes else job


def location_from_slug(url: str, title: str) -> str | None:
    """``/job/Budapest-Sales-Manager-SAP-Solution-BU-1117/1234/`` + "Sales Manager SAP
    Solution" -> ``"Budapest, BU"``: the slug words before and after the title (compared
    on letters and digits only, so "(f/m/d)" matches "fmd"); postal codes are dropped."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if len(segments) < 2 or segments[0] != "job":
        return None
    words = [w for w in _SLUG_WORD.split(unquote(segments[1])) if w]
    needle = "".join(_SLUG_WORD.split(title)).lower()
    if not needle:
        return None
    # word index of every character of the compacted slug
    compact, owner = "", []
    for index, word in enumerate(words):
        compact += word.lower()
        owner += [index] * len(word)
    pos = compact.find(needle)
    if pos < 0:
        return None
    first, last = owner[pos], owner[pos + len(needle) - 1]
    if pos != sum(len(w) for w in words[:first]):  # title starts mid-word: no match
        return None
    before = " ".join(words[:first])
    # postal codes and the "-Job" suffix some sites add are not locations
    after = " ".join(w for w in words[last + 1 :] if not w.isdigit() and w.lower() != "job")
    return ", ".join(p for p in (before, after) if p) or None
