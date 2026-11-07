"""JobMount job boards (generic sitemap + JSON-LD connector).

List: GET {board}/sitemap.xml: one urlset; job URLs /career/{id}/{slug} (``<lastmod>`` on
      some), plus employer/company and search pages, which are skipped.
Job:  JSON-LD JobPosting (validThrough, structured salary).

Board identifier: the board host (``jobnet.wacities.org``). JobMount boards are custom
domains, recognised by the "Powered by JobMount" link (www.jobboardmount.com).
Verified on jobnet.wacities.org and jobs.imdiversity.com (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class JobMountConnector(HostedBoardConnector):
    platform = "jobmount"
    page_fingerprint = re.compile(r"//www\.jobboardmount\.com\b")
    job_path = re.compile(r"/career/(?P<id>\d+)/[^/]*/?")

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}/sitemap.xml"]
