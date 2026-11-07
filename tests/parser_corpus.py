"""Fingerprint of the normaliser's behaviour, used to enforce PARSER_VERSION bumps.

The corpus (``tests/fixtures/parser_corpus.json``) is frozen input: un-normalised
``RawJob`` dicts taken from the real connector fixtures, plus free-text locations,
salaries, remote/employment strings. The fingerprint is a hash of what ``normalize.py``
makes of it. ``tests/test_parser_version.py`` compares it with the committed
``tests/parser_fingerprint.json``.

After a deliberate behaviour change: bump ``normalize.PARSER_VERSION``, then run
``make parser-fingerprint`` (``python -m tests.parser_corpus --write``).
To regenerate the corpus itself from the fixtures: ``python -m tests.parser_corpus --corpus``
(only when adding new kinds of input; it changes the fingerprint too).
"""

import argparse
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jobindex.connectors.base import RawJob
from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    PARSER_VERSION,
    detect_remote_type,
    find_salary,
    html_to_text,
    normalize_country,
    normalize_employment_type,
    parse_datetime,
    parse_location,
    parse_salary,
)

HERE = Path(__file__).resolve().parent
CORPUS_FILE = HERE / "fixtures" / "parser_corpus.json"
FINGERPRINT_FILE = HERE / "parser_fingerprint.json"


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, RemoteType):
        return value.value
    return value


def _decode_raw(data: dict[str, Any]) -> RawJob:
    values: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict) and "$decimal" in value:
            value = Decimal(value["$decimal"])
        elif isinstance(value, dict) and "$datetime" in value:
            value = datetime.fromisoformat(value["$datetime"])
        elif key == "remote_type":
            value = RemoteType(value)
        values[key] = value
    return RawJob(**values)


def build_corpus() -> dict[str, Any]:
    """Collect raw (un-normalised) postings from every connector fixture."""
    from jobindex.connectors.ashby import AshbyConnector
    from jobindex.connectors.greenhouse import GreenhouseConnector
    from jobindex.connectors.lever import LeverConnector
    from jobindex.connectors.recruitee import RecruiteeConnector
    from jobindex.connectors.smartrecruiters import SmartRecruitersConnector
    from jobindex.connectors.workable import WorkableConnector
    from jobindex.connectors.workday import WorkdayConnector

    def fixture(platform: str, name: str) -> Any:
        return json.loads((HERE / "fixtures" / platform / name).read_text(encoding="utf-8"))

    raws: list[RawJob] = []
    raws += [
        GreenhouseConnector._map(j) for j in fixture("greenhouse", "discord_jobs.json")["jobs"]
    ]
    raws += [LeverConnector._map(p) for p in fixture("lever", "spotify_postings.json")]
    raws += [AshbyConnector._map(j) for j in fixture("ashby", "ramp_job_board.json")["jobs"]]
    raws += [
        SmartRecruitersConnector._map("Ubisoft2", p)
        for p in fixture("smartrecruiters", "ubisoft_postings.json")["content"]
    ]
    wk = fixture("workable", "blueground_widget.json")
    raws += [WorkableConnector._map(j, wk["name"]) for j in wk["jobs"]]
    raws += [RecruiteeConnector._map(o) for o in fixture("recruitee", "offers.json")["offers"]]
    base = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    raws += [
        WorkdayConnector._map_list(p, base)
        for p in fixture("workday", "nvidia_jobs_page1.json")["jobPostings"]
    ]
    return {
        "raw_jobs": [{k: _encode(v) for k, v in r.as_dict().items()} for r in raws],
        "locations": [
            "San Francisco, CA",
            "Toronto, ON, Canada",
            "Remote (U.S.)",
            "US, CA, Santa Clara",
            "DE, Munich",
            "Berlin, Germany",
            "London, England",
            "Bengaluru, India",
            "Europe",
            "San Francisco, CA • New York, NY • United States",
            "New York, NY (HQ); Remote",
            "Hybrid - Paris, Île-de-France, France",
            "Sydney NSW, Australia",
            "NYC",
            "Austin, Texas",
            "Remote - EMEA",
            "São Paulo, Brazil",
            "Tokyo, Japan",
        ],
        "salaries": [
            "$120,000 - $150,000",
            "$120k–$150k per year",
            "€50.000 – €60.000 brutto pro Jahr",
            "£40 per hour",
            "CHF 100'000 - 120'000",
            "120-150k USD",
            "₹15,00,000 per annum",
            "The base range for this role is $87,069-$124,385 plus equity.",
            "Fertility HRA (up to $10,000 per year)",
            "We raised $10M",
            "Pay: $25/hr",
        ],
        "remote_texts": ["Remote (US)", "Hybrid - Berlin", "On-site", "Non-remote role", "Austin"],
        "employment_types": [
            "Full-time",
            "FullTime",
            "Permanent",
            "fulltime_fixed_term",
            "Werkstudent",
            "Contractor",
            "Part Time",
            "Intern",
            "Other",
        ],
        "html": ["<p>Build <b>great</b> things.</p><ul><li>Python</li></ul>", "a<br>b"],
        "datetimes": ["2026-11-06T15:01:26.091Z", "2026-10-30 16:32:58 UTC", "2026-11-06"],
        "countries": ["USA", "United Kingdom", "Deutschland", "European Union", "GBR"],
    }


def fingerprint(corpus: dict[str, Any]) -> str:
    """Hash of the normaliser's output over the corpus."""

    def salary(s: Any) -> Any:
        return None if s is None else [str(s.min), str(s.max), s.currency, s.period]

    out = {
        "raw_jobs": [
            {k: _encode(v) for k, v in _decode_raw(r).normalized().as_dict().items()}
            for r in corpus["raw_jobs"]
        ],
        "locations": [
            list(parse_location(x).__getattribute__(f) for f in ("city", "region", "country"))
            for x in corpus["locations"]
        ],
        "parse_salary": [salary(parse_salary(x)) for x in corpus["salaries"]],
        "find_salary": [salary(find_salary(x)) for x in corpus["salaries"]],
        "remote": [detect_remote_type(x).value for x in corpus["remote_texts"]],
        "employment": [normalize_employment_type(x) for x in corpus["employment_types"]],
        "html": [html_to_text(x) for x in corpus["html"]],
        "datetimes": [str(parse_datetime(x)) for x in corpus["datetimes"]],
        "countries": [normalize_country(x) for x in corpus["countries"]],
    }
    data = json.dumps(out, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def load_corpus() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--write", action="store_true", help="record the current fingerprint")
    parser.add_argument("--corpus", action="store_true", help="rebuild the corpus first")
    args = parser.parse_args()
    if args.corpus:
        CORPUS_FILE.write_text(
            json.dumps(build_corpus(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {CORPUS_FILE}")
    digest = fingerprint(load_corpus())
    if args.write or args.corpus:
        FINGERPRINT_FILE.write_text(
            json.dumps({"parser_version": PARSER_VERSION, "digest": digest}, indent=2) + "\n"
        )
        print(f"wrote {FINGERPRINT_FILE}: parser_version={PARSER_VERSION} digest={digest}")
    else:
        print(digest)


if __name__ == "__main__":
    main()
