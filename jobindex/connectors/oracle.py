"""Oracle Recruiting Cloud (Oracle HCM "Candidate Experience" sites; public REST API).

List:   GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
            ?onlyData=true&expand=requisitionList.secondaryLocations
            &finder=findReqs;siteNumber={site},limit=200,offset={n},sortBy=POSTING_DATES_DESC
        -> {"items": [{"TotalJobsCount", "requisitionList": [{Id, Title, PostedDate,
            PrimaryLocation, PrimaryLocationCountry, WorkplaceTypeCode, ShortDescriptionStr,
            secondaryLocations[{Name, CountryCode}]}], ...facets}]}
        200 rows per page is accepted (verified 2026-11-07); deep offsets work.
Detail: GET .../recruitingCEJobRequisitionDetails?expand=all&onlyData=true
            &finder=ById;Id="{id}",siteNumber={site}
        -> {"items": [{ExternalDescriptionStr, ExternalResponsibilitiesStr,
            ExternalQualificationsStr, CorporateDescriptionStr, ExternalPostedStartDate,
            JobSchedule, WorkplaceTypeCode, ...}]}   ("count": 0 once the job is closed)

Board identifier: ``{host}/{siteNumber}``, e.g. ``eeho.fa.us2.oraclecloud.com/CX_45001``.

Completeness: ``TotalJobsCount`` is consistently 1-2 higher than the requisitions the list
returns, on every page and every run (Oracle CX_45001: 2841 of 2843; JPMorgan CX_1001:
7119 of 7120; 2026-11-07) — requisitions that are counted but not published to the site.
We therefore accept a shortfall of at most ``max(TOTAL_SLACK, total / 1000)``; anything
larger is re-paged once and then refused as a partial result.

URL formats recognised:
    {pod}.fa[.{dc}].oraclecloud.com/hcmUI/CandidateExperience/{lang}/sites/{site}[/...]
    {pod}.fa[.{dc}].oraclecloud.com/hcmRestApi/...siteNumber={site}...
Custom-domain career pages: links/redirects to the above (generic HTML detection).
"""

import logging
import re
from dataclasses import replace
from typing import Any
from urllib.parse import unquote

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    normalize_employment_type,
    parse_datetime,
    split_url,
)

log = logging.getLogger(__name__)

PAGE_SIZE = 200
MAX_PAGES = 250  # 50k requisitions
TOTAL_SLACK = 2  # see module docstring
_HOST = re.compile(r"^[a-z0-9-]+\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com$")
_SITE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SITE_IN_TEXT = re.compile(r"siteNumber=([A-Za-z0-9_-]+)")
_WORKPLACE = {
    "ORA_ON_SITE": RemoteType.ONSITE,
    "ORA_REMOTE": RemoteType.REMOTE,
    "ORA_HYBRID": RemoteType.HYBRID,
}


def _split_identifier(board_identifier: str) -> tuple[str, str]:
    host, _, site = board_identifier.partition("/")
    if not _HOST.match(host) or not _SITE.match(site):
        raise ConnectorError(f"oracle: bad board identifier {board_identifier!r}")
    return host, site


def _remote_type(code: Any) -> RemoteType:
    return _WORKPLACE.get(str(code), RemoteType.UNKNOWN)


@register
class OracleConnector(BaseConnector):
    platform = "oracle"
    needs_detail = True
    detail_cache_key = ("title", "location_raw", "posted_at")
    detail_fields = ("description_html", "description_text", "employment_type", "remote_type")

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, query = split_url(url)
        if not _HOST.match(host):
            return None
        site: str | None = None
        lowered = [s.lower() for s in segments]
        if "sites" in lowered and lowered.index("sites") + 1 < len(segments):
            site = segments[lowered.index("sites") + 1]
        elif match := _SITE_IN_TEXT.search(unquote(url)):
            site = match.group(1)
        elif query.get("siteNumber"):
            site = query["siteNumber"][0]
        if not site or not _SITE.match(site):
            return None
        return f"{host}/{site}"

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        host, site = _split_identifier(board_identifier)
        return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/requisitions"

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        rows, total = await self._list(board_identifier)
        if not self._complete(len(rows), total):
            log.info(
                "oracle %s: got %d of %d requisitions, re-paging once",
                board_identifier,
                len(rows),
                total,
            )
            rows, total = await self._list(board_identifier)
            if not self._complete(len(rows), total):
                raise ConnectorError(
                    f"oracle: got {len(rows)} of {total} requisitions; refusing a partial result"
                )
        return [self._map_list(r, board_identifier) for r in rows.values()]

    @staticmethod
    def _complete(found: int, total: int | None) -> bool:
        if total is None:
            return True  # nothing to compare against; paging ended on a short page
        return found >= total - max(TOTAL_SLACK, total // 1000)

    async def _list(self, board_identifier: str) -> tuple[dict[str, dict[str, Any]], int | None]:
        host, site = _split_identifier(board_identifier)
        rows: dict[str, dict[str, Any]] = {}
        total: int | None = None
        for page in range(MAX_PAGES):
            url = (
                f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                "?onlyData=true&expand=requisitionList.secondaryLocations"
                f"&finder=findReqs;siteNumber={site},limit={PAGE_SIZE},"
                f"offset={page * PAGE_SIZE},sortBy=POSTING_DATES_DESC"
            )
            data = await self.http.get_json(url)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or len(items) != 1:
                raise ConnectorError("oracle: list response has no single 'items' entry")
            batch = items[0].get("requisitionList")
            if not isinstance(batch, list):
                raise ConnectorError("oracle: list response has no 'requisitionList'")
            count = items[0].get("TotalJobsCount")
            if isinstance(count, int):
                total = count if total is None else max(total, count)
            for row in batch:
                if row.get("Id") is not None:
                    rows[str(row["Id"])] = row
            if len(batch) < PAGE_SIZE:
                return rows, total
            if page and page % 10 == 0:
                log.info("oracle %s: %d/%s requisitions", board_identifier, len(rows), total)
        raise ConnectorError(f"oracle: more than {MAX_PAGES} pages; refusing a partial result")

    @staticmethod
    def _map_list(row: dict[str, Any], board_identifier: str) -> RawJob:
        host, site = _split_identifier(board_identifier)
        job_id = str(row["Id"])
        locations = [row.get("PrimaryLocation")]
        locations += [loc.get("Name") for loc in row.get("secondaryLocations") or []]
        names = list(dict.fromkeys(str(n) for n in locations if n))
        url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"
        return RawJob(
            external_id=job_id,
            title=str(row.get("Title") or "").strip(),
            location_raw="; ".join(names) or None,
            country=(row.get("PrimaryLocationCountry") or None) if len(names) <= 1 else None,
            remote_type=_remote_type(row.get("WorkplaceTypeCode")),
            posted_at=parse_datetime(row.get("PostedDate")),
            source_url=url,
            apply_url=url,
        )

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        host, site = _split_identifier(board_identifier)
        url = (
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f'?expand=all&onlyData=true&finder=ById;Id="{job.external_id}",siteNumber={site}'
        )
        data = await self.http.get_json(url)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ConnectorError("oracle: detail response has no 'items'")
        if not items:
            return None  # closed between list and detail request
        info = items[0]
        sections = (
            info.get("ExternalDescriptionStr"),
            info.get("ExternalResponsibilitiesStr"),
            info.get("ExternalQualificationsStr"),
            info.get("CorporateDescriptionStr"),  # company boilerplate last
        )
        html = "\n".join(s for s in sections if isinstance(s, str) and s.strip())
        remote_type = _remote_type(info.get("WorkplaceTypeCode"))
        return replace(
            job,
            description_html=html or None,
            description_text=None,
            remote_type=remote_type if remote_type is not RemoteType.UNKNOWN else job.remote_type,
            employment_type=normalize_employment_type(info.get("JobSchedule"))
            or normalize_employment_type(info.get("WorkerType")),
            # posted_at stays the list's PostedDate: it is part of detail_cache_key.
        )
