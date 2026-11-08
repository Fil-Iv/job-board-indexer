"""JobKit job boards (generic sitemap + JSON-LD connector).

List: /sitemap.xml (one urlset with ``<lastmod>``; the home page plus every job page).
Job:  /jobs/{numeric id}-{slug}, JSON-LD JobPosting. Two fields are free text and fixed
      up in ``adjust``: ``baseSalary.value.value`` ("160,000–200,000 + equity", with
      ``currency`` and ``unitText`` alongside) and ``addressLocality``, which holds the
      whole location ("New York, NY (Flatiron)"), so it is parsed as ``location_raw``
      (without the parenthesised note).
      JobKit's REST API needs the board owner's key, so it is not used.

Board identifier: the board host (``jobs.rubyonrails.org``, ``{board}.jobkit.io``).
Custom domains are recognised by ``<meta name="twitter:site" content="@jobkitsite">``.
``*.jobkit.io`` is wildcard DNS: unknown boards answer 404, so the crawl fails loudly.

Only one live board with jobs is known (jobs.rubyonrails.org, verified 2026-11-08); the
eight other ``*.jobkit.io`` boards found list 0 jobs (see coverage/platforms.yaml).
"""

import re
from dataclasses import replace
from typing import Any

from jobindex.connectors.base import RawJob
from jobindex.connectors.generic_jsonld import HostedBoardConnector
from jobindex.connectors.registry import register
from jobindex.normalize import parse_salary

_NOTE = re.compile(r"\s*\([^)]*\)")
_PERIODS = {"HOUR": "hour", "DAY": "day", "WEEK": "week", "MONTH": "month", "YEAR": "year"}


@register
class JobKitConnector(HostedBoardConnector):
    platform = "jobkit"
    native_suffix = ".jobkit.io"
    page_fingerprint = re.compile(r"content=[\"']@jobkitsite[\"']")
    job_path = re.compile(r"/jobs/(?P<id>\d+)-[^/]+/?")

    def adjust(self, job: RawJob, item: dict[str, Any]) -> RawJob:
        changes: dict[str, Any] = {}
        if job.city and "," in job.city and job.location_raw == job.city:
            # "New York, NY (Flatiron)": drop the neighbourhood note, normalized() parses it
            changes.update(city=None, location_raw=_NOTE.sub("", job.city).strip() or None)
        if job.salary_min is None and job.salary_max is None:
            changes.update(_salary(item))
        return replace(job, **changes) if changes else job


def _salary(item: dict[str, Any]) -> dict[str, Any]:
    """``{"currency": "USD", "value": {"value": "160,000–200,000 + equity",
    "unitText": "YEAR"}}`` -> salary fields of RawJob (empty if unparseable)."""
    base = item.get("baseSalary")
    value = base.get("value") if isinstance(base, dict) else None
    text = value.get("value") if isinstance(value, dict) else None
    currency = base.get("currency") if isinstance(base, dict) else None
    if not isinstance(text, str) or not isinstance(currency, str):
        return {}
    salary = parse_salary(f"{currency} {text}")
    if salary is None or (salary.min is None and salary.max is None):
        return {}
    unit = value.get("unitText") if isinstance(value, dict) else None
    period = _PERIODS.get(str(unit).upper()) or salary.period
    return {
        "salary_min": salary.min,
        "salary_max": salary.max,
        "salary_currency": salary.currency or currency,
        "salary_period": period,
    }
