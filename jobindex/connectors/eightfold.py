"""Eightfold AI career sites (generic sitemap + JSON-LD connector).

List: robots.txt -> ``Sitemap: {site}/careers/sitemap_index.xml?domain={domain}`` -> child
      ``/careers/sitemap.xml?domain=...``: every job page with ``<lastmod>``. The category
      sitemap (``sitemap_cat.xml``) is skipped.
Job:  {site}/careers/job/{id}-{slug}?domain={domain}: JSON-LD JobPosting (title,
      description, datePosted, employmentType, hiringOrganization, jobLocation).

Why not the JSON API: ``/api/apply/v2/jobs`` is allowed by robots.txt but answers 403 on
every tenant tried (``coverage/blocked_by_site.yaml``). robots.txt (identical on every
tenant) is ``Disallow: /`` with ``Allow: /careers`` and a few API prefixes; the sitemaps
and job pages are under ``/careers``.

Board identifier: the site host: ``{tenant}.eightfold.ai`` or a custom domain
(``careers.qualcomm.com``), recognised by the career-site assets on
``static.vscdn.net/images/careers/`` + the page's canonical URL.
Verified on paypal.eightfold.ai (305 jobs) and vodafone.eightfold.ai (1,210), 2026-11-08;
careers.qualcomm.com uses the same layout.
"""

import re

from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register


@register
class EightfoldConnector(HostedBoardConnector):
    platform = "eightfold"
    detail_version = 1  # 2026-11-10: addressRegion "KA,IN" -> "KA" (re-read stored pages)
    native_suffix = ".eightfold.ai"
    page_fingerprint = re.compile(r"static\.vscdn\.net/images/careers/")
    child_sitemap = re.compile(r"/careers/sitemap\.xml")
    job_path = re.compile(r"/careers/job/(?P<id>\d+)(?:-[^/]*)?/?")

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        return f"https://{board_identifier}/careers"
