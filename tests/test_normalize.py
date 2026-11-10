from datetime import UTC, datetime
from decimal import Decimal

import pytest

from jobindex.models.enums import RemoteType
from jobindex.normalize import (
    Location,
    content_hash,
    detect_remote_type,
    find_salary,
    html_to_text,
    normalize_country,
    normalize_employment_type,
    normalize_salary_period,
    parse_datetime,
    parse_location,
    parse_salary,
    split_url,
)


def test_html_to_text() -> None:
    html = (
        "<div><h2>About</h2><p>We   build\tthings.</p>"
        "<script>x()</script><ul><li>Python</li><li>SQL &amp; more</li></ul></div>"
    )
    assert html_to_text(html) == "About\nWe build things.\nPython\nSQL & more"
    assert html_to_text("<p>Build <b>great</b> <a href='#'>things</a>.</p><p>Next</p>") == (
        "Build great things.\nNext"
    )
    assert html_to_text("Line one<br>Line two<br/>") == "Line one\nLine two"
    assert html_to_text(None) is None
    assert html_to_text("   ") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("San Francisco, CA", Location("San Francisco", "CA", "US")),
        ("San Francisco, California, United States", Location("San Francisco", "CA", "US")),
        ("Austin, Texas", Location("Austin", "TX", "US")),
        ("Toronto, ON, Canada", Location("Toronto", "ON", "CA")),
        ("Montreal, Quebec", Location("Montreal", "QC", "CA")),
        # Region code + ISO country code: IN/CA/DE are countries here, not US states.
        ("Bengaluru, KA, IN", Location("Bengaluru", "KA", "IN")),
        ("Quebec City, QC, CA", Location("Quebec City", "QC", "CA")),
        ("Nuremberg, BY, DE", Location("Nuremberg", "BY", "DE")),
        ("Newark, DE, US", Location("Newark", "DE", "US")),
        ("Montreal, Québec, Canada", Location("Montreal", "QC", "CA")),
        ("Berlin, Germany", Location("Berlin", None, "DE")),
        ("London, UK", Location("London", None, "GB")),
        ("Bordeaux, Nouvelle-Aquitaine, France", Location("Bordeaux", "Nouvelle-Aquitaine", "FR")),
        ("Remote (U.S.)", Location(None, None, "US")),
        ("Remote - Canada", Location(None, None, "CA")),
        ("Remote", Location()),
        ("Europe", Location()),
        ("NYC", Location("New York", "NY", "US")),
        ("San Francisco Bay Area or Remote (U.S.)", Location("San Francisco", "CA", "US")),
        ("New York, NY (HQ); Remote (Canada)", Location("New York", "NY", "US")),
        ("San Francisco, CA • New York, NY • United States", Location("San Francisco", "CA", "US")),
        ("Hybrid - London, United Kingdom", Location("London", None, "GB")),
        ("Germany", Location(None, None, "DE")),
        ("US, CA, Santa Clara", Location("Santa Clara", "CA", "US")),
        ("DE, Munich", Location("Munich", None, "DE")),
        ("US, CA, Remote", Location(None, "CA", "US")),
        ("California", Location(None, "CA", "US")),
        (None, Location()),
    ],
)
def test_parse_location(raw: str | None, expected: Location) -> None:
    assert parse_location(raw) == expected


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("United States", "US"),
        ("USA", "US"),
        ("us", "US"),
        ("Deutschland", "DE"),
        ("United Kingdom", "GB"),
        ("GBR", "GB"),
        ("European Union", None),
        ("", None),
    ],
)
def test_normalize_country(value: str, code: str | None) -> None:
    assert normalize_country(value) == code


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("Remote (US)",), RemoteType.REMOTE),
        (("New York, NY", "Senior Engineer (Hybrid)"), RemoteType.HYBRID),
        (("On-site - Berlin",), RemoteType.ONSITE),
        (("Non-remote role, Austin",), RemoteType.ONSITE),
        (("Work from home",), RemoteType.REMOTE),
        (("Berlin", "Engineer"), RemoteType.UNKNOWN),
        ((None,), RemoteType.UNKNOWN),
    ],
)
def test_detect_remote_type(texts: tuple[str | None, ...], expected: RemoteType) -> None:
    assert detect_remote_type(*texts) == expected


D = Decimal


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$120,000 - $150,000", (D(120000), D(150000), "USD", "year")),
        ("$120k–$150k per year", (D(120000), D(150000), "USD", "year")),
        ("$211.4K - $290.6K", (D("211400.0"), D("290600.0"), "USD", "year")),
        ("120-150k USD", (D(120000), D(150000), "USD", "year")),
        ("€50.000 – €60.000 brutto pro Jahr", (D(50000), D(60000), "EUR", "year")),
        ("EUR 4.500 per month", (D(4500), D(4500), "EUR", "month")),
        ("£40 per hour", (D(40), D(40), "GBP", "hour")),
        ("$25 - $30/hr", (D(25), D(30), "USD", "hour")),
        ("CHF 100'000 - 120'000", (D(100000), D(120000), "CHF", "year")),
        ("C$90,000 to C$110,000 annually", (D(90000), D(110000), "CAD", "year")),
        ("1,500.50 USD weekly", (D("1500.50"), D("1500.50"), "USD", "week")),
    ],
)
def test_parse_salary(text: str, expected: tuple[Decimal, Decimal, str, str]) -> None:
    salary = parse_salary(text)
    assert salary is not None
    assert (salary.min, salary.max, salary.currency, salary.period) == expected


def test_find_salary_in_description() -> None:
    spotify = (
        "The United States base range for this position is $87,069-$124,385 plus equity. "
        "The benefits available for this position include health insurance."
    )
    salary = find_salary(spotify)
    assert salary is not None
    assert (salary.min, salary.max, salary.currency) == (D(87069), D(124385), "USD")

    assert find_salary("Pay: $40 - $45 per hour, plus overtime") is not None


@pytest.mark.parametrize(
    "text",
    [
        "- Fertility HRA (up to $10,000 per year)",  # benefit, not salary
        "We have raised $10M from top investors.",
        "Over $275B in annualized spend flows through Ramp.",
        "401(k) plan with 4% match",
        "Learning budget of $1,000 per year",
        "We are 120 - 150 people.",  # no currency
    ],
)
def test_find_salary_rejects_non_salaries(text: str) -> None:
    assert find_salary(text) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Full-time", "full_time"),
        ("FullTime", "full_time"),
        ("Permanent", "full_time"),
        ("Part Time", "part_time"),
        ("Intern", "internship"),
        ("Contractor", "contract"),
        ("Short Term", "temporary"),
        ("Temporary", "temporary"),
        ("Werkstudent", "internship"),
        ("Something else", None),
        (None, None),
    ],
)
def test_normalize_employment_type(value: str | None, expected: str | None) -> None:
    assert normalize_employment_type(value) == expected


def test_normalize_salary_period() -> None:
    assert normalize_salary_period("1 YEAR") == "year"
    assert normalize_salary_period("per-hour-wage") == "hour"
    assert normalize_salary_period("Base Salary") is None


def test_content_hash_ignores_cosmetic_fields_and_key_order() -> None:
    a = {"title": "Engineer", "salary_min": D("100.00"), "source_url": "https://a?utm=1"}
    b = {"source_url": "https://a?utm=2", "salary_min": D("100"), "title": "Engineer"}
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash({**a, "title": "Senior Engineer"})


def test_parse_datetime() -> None:
    assert parse_datetime("2026-11-06T15:01:26.091Z") == datetime(
        2026, 11, 6, 15, 1, 26, 91000, tzinfo=UTC
    )
    assert parse_datetime(1792767640768) == datetime(2026, 10, 23, 15, 0, 40, 768000, tzinfo=UTC)
    assert parse_datetime("2026-11-03T11:27:16-05:00") == datetime(
        2026, 11, 3, 16, 27, 16, tzinfo=UTC
    )
    assert parse_datetime("2026-10-30 16:32:58 UTC") == datetime(
        2026, 10, 30, 16, 32, 58, tzinfo=UTC
    )
    assert parse_datetime("not a date") is None
    assert parse_datetime(None) is None


def test_split_url() -> None:
    assert split_url("www.Example.com/a%20b/c?x=1") == ("example.com", ["a b", "c"], {"x": ["1"]})
