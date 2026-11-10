import json
from datetime import UTC, datetime
from decimal import Decimal

from jobindex.models.enums import RemoteType
from jobindex.schemaorg import count_job_postings, extract_job_postings, is_expired, to_raw_job
from tests.fakes import load_fixture

JSONLD_PAGE = """<html><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
  {"@type": "WebPage", "name": "x"},
  {"@type": ["JobPosting"], "title": "Senior Rails Developer",
   "description": "&lt;p&gt;Build &lt;b&gt;things&lt;/b&gt;&lt;/p&gt;",
   "datePosted": "2026-10-01", "validThrough": "2026-12-31T00:00:00Z",
   "employmentType": ["FULL_TIME", "CONTRACTOR"],
   "hiringOrganization": {"@type": "Organization", "name": "Acme"},
   "jobLocationType": "TELECOMMUTE",
   "jobLocation": [{"@type": "Place", "address": {"@type": "PostalAddress",
       "addressLocality": "Berlin", "addressCountry": "DE"}}],
   "baseSalary": {"@type": "MonetaryAmount", "currency": "EUR",
       "value": {"@type": "QuantitativeValue", "minValue": 70000, "maxValue": 90000,
                 "unitText": "YEAR"}},
   "url": "https://jobs.example/rails-dev"}
]}</script>
<script type="application/ld+json">{ not json </script>
</head><body></body></html>"""


def test_jsonld_with_graph_and_bad_blocks() -> None:
    items = extract_job_postings(JSONLD_PAGE)
    assert len(items) == 1
    assert count_job_postings(JSONLD_PAGE) == (1, 0)
    raw = to_raw_job(items[0], external_id="1", source_url="https://jobs.example/rails-dev")
    assert raw.title == "Senior Rails Developer"
    assert raw.company == "Acme"
    assert raw.description_html == "<p>Build <b>things</b></p>"
    assert (raw.city, raw.country) == ("Berlin", "DE")
    assert raw.remote_type is RemoteType.REMOTE
    assert raw.employment_type == "full_time"
    assert (raw.salary_min, raw.salary_max, raw.salary_currency, raw.salary_period) == (
        Decimal(70000),
        Decimal(90000),
        "EUR",
        "year",
    )
    assert raw.posted_at is not None
    assert raw.posted_at.date().isoformat() == "2026-10-01"
    assert not is_expired(items[0], now=datetime(2026, 11, 7, tzinfo=UTC))
    assert is_expired(items[0], now=datetime(2027, 1, 2, tzinfo=UTC))


def test_microdata_from_real_smartrecruiters_page() -> None:
    html = load_fixture("smartrecruiters", "job_744000160228509.html")
    assert count_job_postings(html) == (0, 1)
    item = extract_job_postings(html)[0]
    raw = to_raw_job(item, external_id="744000160228509", source_url=None)
    assert raw.title == "Superviseur·e Texture & LookDev - Hybride VFX"
    assert raw.company == "Ubisoft"
    assert (raw.city, raw.region, raw.country) == ("Montreal", "Québec", "Canada")
    assert raw.employment_type == "full_time"  # "Temps plein"
    assert raw.description_html is not None
    # Nested items' properties stay inside them (address is not a JobPosting property).
    assert "addressLocality" not in item


def test_no_markup() -> None:
    assert extract_job_postings("<html><body><h1>Jobs</h1></body></html>") == []


def _job_at(address: str) -> str:
    return (
        '{"@type": "JobPosting", "title": "T", "jobLocation": {"@type": "Place", '
        f'"address": {{"@type": "PostalAddress", {address}}}}}}}'
    )


def test_region_repeating_the_country_code_is_trimmed() -> None:
    """Eightfold writes ``addressRegion: "KA,IN"`` (paypal.eightfold.ai, 2026-11-10)."""
    item = json.loads(
        _job_at(
            '"addressLocality": "Bengaluru", "addressRegion": "KA,IN", '
            '"addressCountry": {"@type": "Country", "name": "IN"}'
        )
    )
    raw = to_raw_job(item, external_id="1", source_url=None).normalized()
    assert (raw.city, raw.region, raw.country) == ("Bengaluru", "KA", "IN")
    assert raw.location_raw == "Bengaluru, KA, IN"


def test_whole_location_in_address_locality_is_split() -> None:
    """JBoard puts "City, Region, Country" into addressLocality (couriergigs.com, 2026-11-10)."""
    for locality, expected in [
        ("West Medford, Massachusetts, United States", ("West Medford", "MA", "US")),
        ("Quebec City, QC, CA", ("Quebec City", "QC", "CA")),
    ]:
        item = json.loads(_job_at(f'"addressLocality": "{locality}"'))
        raw = to_raw_job(item, external_id="1", source_url=None).normalized()
        assert (raw.city, raw.region, raw.country) == expected
