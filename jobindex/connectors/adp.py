"""ADP Workforce Now career centers (public JSON behind the career-center SPA).

List:   GET https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/
            v1/job-requisitions?cid={cid}&lang=en_US&locale=en_US&$top=100&$skip={start}
        -> {meta: {startSequence, totalNumber, links}, jobRequisitions: [{itemID,
            requisitionTitle, postDate, requisitionLocations[{address{cityName,
            countrySubdivisionLevel1{codeValue}}, nameCode{shortName}}], workLevelCode
            {shortName: FullTime|PartTime|...}, payGradeRange{minimumRate, maximumRate},
            customFieldGroup{stringFields: ExternalJobID, SalaryType, ...}}]}
        ``$skip`` is the 1-based position of the first item (``$skip=3`` starts with the
        3rd job, overlapping a ``$skip=0..2`` page), so pages are requested from
        ``1 + jobs so far``, de-duplicated by itemID, and the result must contain
        ``totalNumber`` jobs. No cookies needed (verified 2026-11-07). Unknown cids: 404.
Detail: GET .../job-requisitions/{itemID}?cid={cid}&lang=en_US&locale=en_US
        -> the same fields + ``requisitionDescription`` (HTML). Only for new/changed jobs.
robots.txt on workforcenow.adp.com is an HTML redirector page (no rules).

Board identifier: the career-center ``cid`` (uuid, lower case), plus ``:{lang}`` when the
career center is not in ``en_US`` (``8d16626f-...:en_CA``): postings exist per language, and
the Canadian example lists 3 jobs with ``lang=en_CA`` and none with ``en_US``. ``lang`` is
taken from the board URL.

URL formats recognised:
    workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={uuid}
    workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/
        job-requisitions?cid={uuid}
Not covered: ADP Recruiting Management (recruiting.adp.com) and myjobs.adp.com.

Verified on cids d4d64eb4-... (US, 7 jobs) and 8d16626f-... (Canada, 3 jobs), 2026-11-07, and
9e26f425-... (nou Systems, US, 43 jobs), 2026-11-08.
"""

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from jobindex.connectors.base import BaseConnector, ConnectorError, RawJob
from jobindex.connectors.registry import register
from jobindex.http import FetchError
from jobindex.normalize import (
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    split_url,
)

HOST = "workforcenow.adp.com"
API = f"https://{HOST}/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
PAGE_SIZE = 100
MAX_PAGES = 100
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LANG = re.compile(r"^[a-z]{2}_[A-Z]{2}$")
DEFAULT_LANG = "en_US"


def _board(board_identifier: str) -> tuple[str, str]:
    """``cid[:lang]`` -> (cid, lang)."""
    cid, _, lang = board_identifier.partition(":")
    return cid, lang or DEFAULT_LANG


def _params(board_identifier: str) -> dict[str, str]:
    cid, lang = _board(board_identifier)
    return {"cid": cid, "lang": lang, "locale": lang}


def _amount(rate: Any) -> Decimal | None:
    value = rate.get("amountValue") if isinstance(rate, dict) else None
    try:
        amount = Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None
    return amount if amount else None  # 0.0 = "no minimum"


def _custom(item: dict[str, Any], name: str) -> str | None:
    fields = item.get("customFieldGroup") or {}
    for field in (fields.get("stringFields") or []) + (fields.get("codeFields") or []):
        if (field.get("nameCode") or {}).get("codeValue") == name:
            value = field.get("stringValue") or field.get("shortName")
            return str(value).strip() if value else None
    return None


@register
class ADPConnector(BaseConnector):
    platform = "adp"
    needs_detail = True

    @classmethod
    def detect(cls, url: str) -> str | None:
        host, segments, query = split_url(url)
        if host != HOST or segments[:2] != ["mascsr", "default"]:
            return None
        lowered = {k.lower(): v[0] for k, v in query.items() if v}
        cid = lowered.get("cid", "").lower()
        if not _UUID.match(cid):
            return None
        lang = lowered.get("lang", "")
        lang = lang[:2].lower() + "_" + lang[3:].upper() if len(lang) == 5 else lang
        return f"{cid}:{lang}" if _LANG.match(lang) and lang != DEFAULT_LANG else cid

    @classmethod
    def board_url(cls, board_identifier: str) -> str:
        cid, lang = _board(board_identifier)
        return (
            f"https://{HOST}/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&lang={lang}"
        )

    async def fetch_jobs(self, board_identifier: str) -> list[RawJob]:
        jobs: dict[str, RawJob] = {}
        total: int | None = None
        for _ in range(MAX_PAGES):
            params = {**_params(board_identifier), "$top": PAGE_SIZE, "$skip": len(jobs) + 1}
            data = await self.http.get_json(API, params=params)
            items = data.get("jobRequisitions") if isinstance(data, dict) else None
            meta = data.get("meta") if isinstance(data, dict) else None
            if not isinstance(items, list) or not isinstance(meta, dict):
                raise ConnectorError(f"adp: unexpected response for {board_identifier}")
            total = int(meta.get("totalNumber") or 0)
            before = len(jobs)
            for item in items:
                job = self._map(item, board_identifier)
                jobs.setdefault(job.external_id, job)
            if len(jobs) >= total:
                break
            if len(jobs) == before:
                raise ConnectorError(
                    f"adp: {board_identifier} reports {total} jobs, page added none ({before})"
                )
        else:
            raise ConnectorError(f"adp: over {MAX_PAGES} pages for {board_identifier}")
        if len(jobs) != total:
            raise ConnectorError(f"adp: {board_identifier} reports {total}, got {len(jobs)}")
        return list(jobs.values())

    async def fetch_job_detail(self, board_identifier: str, job: RawJob) -> RawJob | None:
        try:
            data = await self.http.get_json(
                f"{API}/{job.external_id}", params=_params(board_identifier)
            )
        except FetchError as exc:
            if exc.status_code in (404, 410):
                return None
            raise
        if not isinstance(data, dict) or data.get("itemID") != job.external_id:
            raise ConnectorError(f"adp: unexpected detail for {job.external_id}")
        return replace(job, description_html=data.get("requisitionDescription") or None)

    @staticmethod
    def _map(item: dict[str, Any], board_identifier: str) -> RawJob:
        if not item.get("itemID"):
            raise ConnectorError("adp: requisition without itemID")
        item_id = str(item["itemID"])
        locations = [loc for loc in item.get("requisitionLocations") or [] if isinstance(loc, dict)]
        names = [((loc.get("nameCode") or {}).get("shortName") or "").strip() for loc in locations]
        address = (locations[0].get("address") or {}) if len(locations) == 1 else {}
        pay = item.get("payGradeRange") or {}
        low, high = _amount(pay.get("minimumRate")), _amount(pay.get("maximumRate"))
        rate = pay.get("maximumRate") or pay.get("minimumRate") or {}
        has_pay = low is not None or high is not None
        url = (
            f"{ADPConnector.board_url(board_identifier)}"
            f"&jobId={_custom(item, 'ExternalJobID') or item_id}"
        )
        return RawJob(
            external_id=item_id,
            title=str(item.get("requisitionTitle") or "").strip(),
            location_raw="; ".join(n for n in names if n) or None,
            city=address.get("cityName") or None,
            region=(address.get("countrySubdivisionLevel1") or {}).get("codeValue") or None,
            employment_type=normalize_employment_type(
                (item.get("workLevelCode") or {}).get("shortName")
            ),
            salary_min=low,
            salary_max=high,
            salary_currency=(rate.get("currencyCode") or None) if has_pay else None,
            salary_period=normalize_salary_period(_custom(item, "SalaryType")) if has_pay else None,
            posted_at=parse_datetime(item.get("postDate")),
            source_url=url,
            apply_url=url,
        )
