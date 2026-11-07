"""Jobiqo job boards (generic sitemap + JSON-LD connector).

List: GET {board}/sitemap.xml (index of ?page=N files: jobs, organisations, blog, with
      ``<lastmod>``). robots.txt also declares /search/sitemap.xml and
      /jobiqo_seo_search/index.xml (256 files of search pages on thejobnetwork.com); their
      job URLs are the same as the main sitemap's (4730/4730 on 2026-11-07), so they are not read.
Job:  /job/{slug}-{numeric id} (newer) or /job/{uuid}/{slug} (older), JSON-LD JobPosting
      with validThrough and structured salary.

Board identifier: the board host (``www.lovelocaljobs.com``). Jobiqo boards live on custom
domains only; they are recognised by the "powered by" link to www.jobiqo.com in the page
config.
Verified on www.lovelocaljobs.com and www.thejobnetwork.com (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


@register
class JobiqoConnector(HostedBoardConnector):
    platform = "jobiqo"
    page_fingerprint = re.compile(r"//www\.jobiqo\.com\b")
    job_path = re.compile(rf"/job/(?:[^/]*-(?P<id>\d{{3,}})|(?P<uuid>{_UUID})/[^/]*)/?")

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}/sitemap.xml"]
