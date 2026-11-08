"""JobMount job boards (generic sitemap + JSON-LD connector).

List: GET {board}/sitemap.xml: one urlset; job URLs /career/{id}/{slug} (``<lastmod>`` on
      some), plus employer/company and search pages, which are skipped.
Job:  JSON-LD JobPosting (validThrough, structured salary). addressRegion is
      "{name} - {code}" on some boards ("Texas - TX", imdiversity): the code is kept, so
      location search for "Austin, TX" matches.

Board identifier: the board host (``jobnet.wacities.org``). JobMount boards are custom
domains, recognised by the "Powered by JobMount" link (www.jobboardmount.com).
Verified on jobnet.wacities.org and jobs.imdiversity.com (2026-11-07).
"""

import re
from dataclasses import replace
from typing import Any

from jobindex.connectors.base import RawJob
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register

_NAMED_REGION = re.compile(r"^.+\s-\s(?P<code>[A-Z]{2,3})$")  # "Texas - TX"


@register
class JobMountConnector(HostedBoardConnector):
    platform = "jobmount"
    detail_version = 1  # 2026-11-08: "Texas - TX" regions -> "TX"
    page_fingerprint = re.compile(r"//www\.jobboardmount\.com\b")
    job_path = re.compile(r"/career/(?P<id>\d+)/[^/]*/?")

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}/sitemap.xml"]

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        match = _NAMED_REGION.match(job.region or "")
        return replace(job, region=match["code"]) if match else job
