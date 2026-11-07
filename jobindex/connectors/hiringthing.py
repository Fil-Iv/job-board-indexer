"""HiringThing career sites (generic sitemap + JSON-LD connector, list from the RSS feed).

List: GET https://{company}.hiringthing.com/api/rss.xml   (linked from the board page)
      RSS 2.0: one <item> per open job (title, link /job/{id}/{slug}, location, full
      description); it matched the board page exactly (franklin-companies 41/41,
      2026-11-07). An empty board is an empty channel; unknown companies answer 403.
      The sitemap named in robots.txt is a global index on S3 with one file per internal
      company id (2,800+), not addressable by subdomain, so it is not used.
Job:  GET https://{company}.hiringthing.com/job/{id}/{slug}: JSON-LD JobPosting (title,
      description, datePosted, hiringOrganization, jobLocation with street address).
      The REST API needs the customer's key.

Board identifier: the board host (``franklin-companies.hiringthing.com``). HiringThing is
also white-labelled by partners under other domains; those are not recognised yet.
Verified on franklin-companies and friendsofthechildren-national (2026-11-07).
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class HiringThingConnector(HostedBoardConnector):
    platform = "hiringthing"
    native_suffix = ".hiringthing.com"
    job_path = re.compile(r"/job/(?P<id>\d+)(?:/[^/]*)?/?")

    async def sitemap_roots(self, board_identifier: str) -> list[str]:
        return []  # the robots.txt sitemap is a global index keyed by internal id

    async def feed_urls(self, board_identifier: str) -> list[str]:
        return [f"https://{board_identifier}/api/rss.xml"]
