"""iSolved Hire career sites (generic sitemap + JSON-LD connector).

Same software as ApplicantPro (see jobindex.connectors.applicantpro), on isolvedhire.com:
GET https://{tenant}.isolvedhire.com/job_site_map.xml lists the tenant's open jobs as
/jobs/{id}.html with ``<lastmod>``; each page has a JSON-LD JobPosting. The tenant's
/sitemap.xml (13k URLs on isolved.isolvedhire.com) is search/browse pages only, and the
robots.txt index (feeds.isolvedhire.com) lists the job sitemaps of all ~7400 tenants.

Board identifier: the tenant subdomain (``davidsonoil``).
Verified on davidsonoil and isolved (2026-11-07).
"""

from jobindex.connectors.applicantpro import ApplicantProConnector
from jobindex.connectors.registry import register


@register
class ISolvedHireConnector(ApplicantProConnector):
    platform = "isolvedhire"
    domain = "isolvedhire.com"
